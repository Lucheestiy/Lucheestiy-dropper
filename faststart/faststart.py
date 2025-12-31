import os
import struct
import subprocess
import sys
import time
import json
import sqlite3
from pathlib import Path


def log(message: str) -> None:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"{now} droppr-faststart: {message}", flush=True)


VIDEO_META_DB_PATH = os.environ.get("DROPPR_VIDEO_META_DB_PATH", "/database/droppr-video-meta.sqlite3")
WATCH_DIR = os.environ.get("WATCH_DIR", "/srv")
FFPROBE_TIMEOUT_SECONDS = int(os.environ.get("DROPPR_VIDEO_META_FFPROBE_TIMEOUT_SECONDS", "30"))
FASTSTART_FFMPEG_TIMEOUT_SECONDS = int(os.environ.get("DROPPR_FASTSTART_FFMPEG_TIMEOUT_SECONDS", "7200"))
FASTSTART_X264_PRESET = (os.environ.get("DROPPR_FASTSTART_X264_PRESET", "slow") or "slow").strip()
FASTSTART_X264_CRF = int(os.environ.get("DROPPR_FASTSTART_X264_CRF", "16"))
FASTSTART_AAC_BITRATE = (os.environ.get("DROPPR_FASTSTART_AAC_BITRATE", "256k") or "256k").strip()
FASTSTART_COPY_AUDIO = (os.environ.get("DROPPR_FASTSTART_COPY_AUDIO", "true") or "true").strip().lower() in (
    "1",
    "true",
    "yes",
    "y",
    "on",
)


def _clamp_int(value: int, *, min_value: int, max_value: int) -> int:
    return max(min_value, min(int(value), max_value))


def _safe_rel_db_path(abs_path: Path) -> str:
    try:
        rel = abs_path.resolve().relative_to(Path(WATCH_DIR).resolve())
    except Exception:
        return "/" + abs_path.name
    return "/" + rel.as_posix().lstrip("/")


def _ensure_video_meta_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS video_meta (
            path TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            action TEXT,
            error TEXT,
            uploaded_at INTEGER,
            processed_at INTEGER,
            original_size INTEGER,
            processed_size INTEGER,
            original_meta_json TEXT,
            processed_meta_json TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_video_meta_status ON video_meta(status)")
    conn.commit()


def _with_video_meta_conn():
    if not VIDEO_META_DB_PATH:
        return None
    try:
        os.makedirs(os.path.dirname(VIDEO_META_DB_PATH), exist_ok=True)
        conn = sqlite3.connect(VIDEO_META_DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        _ensure_video_meta_db(conn)
        return conn
    except Exception as exc:
        log(f"warning: failed to open video meta db: {exc}")
        return None


def _db_get_video_meta_row(path_key: str) -> dict | None:
    conn = _with_video_meta_conn()
    if conn is None:
        return None

    try:
        row = conn.execute(
            """
            SELECT
                path,
                status,
                action,
                error,
                uploaded_at,
                processed_at,
                original_size,
                processed_size,
                original_meta_json,
                processed_meta_json
            FROM video_meta
            WHERE path = ?
            LIMIT 1
            """,
            (path_key,),
        ).fetchone()
        if row is None:
            return None
        return {k: row[k] for k in row.keys()}
    except Exception as exc:
        log(f"warning: failed to read video meta for {path_key}: {exc}")
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _coerce_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _get_recorded_processed_fingerprint(row: dict) -> tuple[int | None, int | None]:
    processed_size = _coerce_int(row.get("processed_size"))
    processed_mtime = None

    try:
        raw = row.get("processed_meta_json")
        if raw:
            meta = json.loads(raw)
            if isinstance(meta, dict):
                processed_mtime = _coerce_int(meta.get("mtime"))
                if processed_size is None:
                    processed_size = _coerce_int(meta.get("size"))
    except Exception:
        processed_mtime = None

    if processed_size is None:
        processed_size = _coerce_int(row.get("original_size"))

    return processed_size, processed_mtime


def _db_upsert_video_meta(
    *,
    path_key: str,
    status: str,
    action: str | None,
    error: str | None,
    uploaded_at: int | None,
    processed_at: int | None,
    original_size: int | None,
    processed_size: int | None,
    original_meta: dict | None,
    processed_meta: dict | None,
) -> None:
    conn = _with_video_meta_conn()
    if conn is None:
        return

    try:
        conn.execute(
            """
            INSERT INTO video_meta (
                path, status, action, error,
                uploaded_at, processed_at,
                original_size, processed_size,
                original_meta_json, processed_meta_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                status=excluded.status,
                action=excluded.action,
                error=excluded.error,
                uploaded_at=excluded.uploaded_at,
                processed_at=excluded.processed_at,
                original_size=excluded.original_size,
                processed_size=excluded.processed_size,
                original_meta_json=excluded.original_meta_json,
                processed_meta_json=excluded.processed_meta_json
            """,
            (
                path_key,
                status,
                action,
                error,
                uploaded_at,
                processed_at,
                original_size,
                processed_size,
                json.dumps(original_meta, separators=(",", ":"), sort_keys=True) if original_meta is not None else None,
                json.dumps(processed_meta, separators=(",", ":"), sort_keys=True) if processed_meta is not None else None,
            ),
        )
        conn.commit()
    except Exception as exc:
        log(f"warning: failed to write video meta for {path_key}: {exc}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _parse_ratio(value: str | None) -> float | None:
    if not value:
        return None
    value = str(value).strip()
    if not value or value == "0/0":
        return None
    if "/" not in value:
        try:
            return float(value)
        except Exception:
            return None
    num_s, den_s = value.split("/", 1)
    try:
        num = float(num_s)
        den = float(den_s)
    except Exception:
        return None
    if den == 0:
        return None
    return num / den


def _normalize_ffprobe_meta(raw: dict, *, size_bytes: int | None, mtime: int | None) -> dict:
    streams = raw.get("streams") if isinstance(raw.get("streams"), list) else []
    fmt = raw.get("format") if isinstance(raw.get("format"), dict) else {}

    video_stream = next(
        (s for s in streams if isinstance(s, dict) and s.get("codec_type") == "video"),
        None,
    )
    audio_stream = next(
        (s for s in streams if isinstance(s, dict) and s.get("codec_type") == "audio"),
        None,
    )

    duration = None
    try:
        duration = float(fmt.get("duration")) if fmt.get("duration") is not None else None
    except Exception:
        duration = None

    bit_rate = None
    try:
        bit_rate = int(fmt.get("bit_rate")) if fmt.get("bit_rate") is not None else None
    except Exception:
        bit_rate = None

    rotation = None
    if isinstance(video_stream, dict):
        tags = video_stream.get("tags") if isinstance(video_stream.get("tags"), dict) else {}
        rot_raw = tags.get("rotate")
        if rot_raw is not None:
            try:
                rotation = int(str(rot_raw).strip())
            except Exception:
                rotation = None

    width = int(video_stream.get("width") or 0) if isinstance(video_stream, dict) else 0
    height = int(video_stream.get("height") or 0) if isinstance(video_stream, dict) else 0
    disp_w, disp_h = width or None, height or None
    if rotation in {90, 270} and width and height:
        disp_w, disp_h = height, width

    fps = None
    if isinstance(video_stream, dict):
        fps = _parse_ratio(video_stream.get("avg_frame_rate")) or _parse_ratio(video_stream.get("r_frame_rate"))

    return {
        "size": size_bytes,
        "mtime": mtime,
        "container": fmt.get("format_name"),
        "duration": duration,
        "bit_rate": bit_rate,
        "video": {
            "codec": video_stream.get("codec_name") if isinstance(video_stream, dict) else None,
            "profile": video_stream.get("profile") if isinstance(video_stream, dict) else None,
            "pix_fmt": video_stream.get("pix_fmt") if isinstance(video_stream, dict) else None,
            "width": width or None,
            "height": height or None,
            "display_width": disp_w,
            "display_height": disp_h,
            "fps": fps,
            "rotation": rotation,
        },
        "audio": {
            "codec": audio_stream.get("codec_name") if isinstance(audio_stream, dict) else None,
            "channels": (int(audio_stream.get("channels") or 0) or None) if isinstance(audio_stream, dict) else None,
            "sample_rate": (
                int(audio_stream.get("sample_rate") or 0) or None
            )
            if isinstance(audio_stream, dict)
            else None,
        },
    }


def probe_video_meta(path: Path) -> dict | None:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    except Exception:
        st = None

    size_bytes = int(st.st_size) if st else None
    mtime = int(st.st_mtime) if st else None

    cmd = [
        "ffprobe",
        "-hide_banner",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=FFPROBE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return None

    if result.returncode != 0 or not result.stdout.strip():
        return None

    try:
        raw = json.loads(result.stdout)
    except Exception:
        return None

    if not isinstance(raw, dict):
        return None

    return _normalize_ffprobe_meta(raw, size_bytes=size_bytes, mtime=mtime)


def wait_for_stable_size(path: Path, *, interval_seconds: float = 2.0, timeout_seconds: float = 120.0) -> bool:
    deadline = time.time() + timeout_seconds
    last_size: int | None = None
    stable_count = 0

    while time.time() < deadline:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False

        if size == last_size and size > 0:
            stable_count += 1
            if stable_count >= 2:
                return True
        else:
            stable_count = 0
            last_size = size

        time.sleep(interval_seconds)

    return False


def find_top_level_atom_offsets(path: Path) -> dict[str, int]:
    offsets: dict[str, int] = {}

    with path.open("rb") as f:
        file_size = os.fstat(f.fileno()).st_size
        offset = 0

        while offset + 8 <= file_size:
            header = f.read(8)
            if len(header) < 8:
                break

            atom_size = struct.unpack(">I", header[:4])[0]
            atom_type = header[4:8].decode("ascii", errors="replace")
            header_size = 8

            if atom_size == 1:
                ext = f.read(8)
                if len(ext) < 8:
                    break
                atom_size = struct.unpack(">Q", ext)[0]
                header_size = 16
            elif atom_size == 0:
                atom_size = file_size - offset

            if atom_type in ("moov", "mdat") and atom_type not in offsets:
                offsets[atom_type] = offset
                if "moov" in offsets and "mdat" in offsets:
                    return offsets

            if atom_size < header_size:
                break

            f.seek(atom_size - header_size, 1)
            offset += atom_size

    return offsets


def get_video_codec(path: Path) -> str | None:
    """Get the video codec of a file using ffprobe."""
    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return result.stdout.strip().lower()
    except Exception:
        pass
    return None


def has_extra_data_streams(path: Path) -> bool:
    """Check if video has extra data streams that can cause playback issues."""
    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "stream=codec_type",
            "-of", "csv=p=0",
            str(path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            streams = result.stdout.strip().split('\n')
            data_count = sum(1 for s in streams if s == 'data' or s == 'unknown')
            if data_count > 0:
                return True
    except Exception:
        pass
    return False


def has_timestamp_errors(path: Path) -> bool:
    """Check if video has timestamp/dts errors that cause playback issues."""
    try:
        cmd = [
            "ffmpeg",
            "-v", "error",
            "-i", str(path),
            "-f", "null",
            "-t", "10",  # Only check first 10 seconds for speed
            "-",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        stderr = result.stderr.lower()
        # Check for common timestamp issues
        if "non monotonically increasing dts" in stderr:
            return True
        if "invalid dts" in stderr:
            return True
        if "discarding invalid" in stderr:
            return True
    except Exception:
        pass
    return False


def fix_video_errors(path: Path) -> bool:
    """Fix playback issues by remuxing (preferred) or re-encoding (fallback)."""
    tmp_path = path.with_name(f".{path.stem}.fixed{path.suffix}")
    try:
        st = path.stat()
    except FileNotFoundError:
        return False

    try:
        # Try a lossless remux first (keeps original video quality).
        # This also drops extra streams (e.g., iPhone metadata/data tracks) by only mapping v:0 + a:?.
        remux_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-fflags",
            "+genpts",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-sn",
            "-dn",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(tmp_path),
        ]

        log(f"fixing video errors (remux): {path.name}")
        remux_result = subprocess.run(remux_cmd, capture_output=True, text=True, timeout=FASTSTART_FFMPEG_TIMEOUT_SECONDS)
        if remux_result.returncode == 0 and not has_extra_data_streams(tmp_path) and not has_timestamp_errors(tmp_path):
            pass
        else:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

            # Remux failed; fall back to high-quality re-encode.
            crf = _clamp_int(FASTSTART_X264_CRF, min_value=0, max_value=51)
            base_cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(path),
                "-map",
                "0:v:0",  # Only first video stream
                "-map",
                "0:a:0?",  # Only first audio stream (optional)
                "-sn",
                "-dn",
                "-c:v",
                "libx264",
                "-preset",
                FASTSTART_X264_PRESET,
                "-crf",
                str(crf),
                "-pix_fmt",
                "yuv420p",
                "-profile:v",
                "high",
                "-movflags",
                "+faststart",
                str(tmp_path),
            ]

            log(f"fixing video errors (re-encode): {path.name}")

            attempts: list[list[str]] = []
            if FASTSTART_COPY_AUDIO:
                attempts.append(base_cmd[:-3] + ["-c:a", "copy"] + base_cmd[-3:])
            attempts.append(base_cmd[:-3] + ["-c:a", "aac", "-b:a", FASTSTART_AAC_BITRATE] + base_cmd[-3:])

            last_err: str | None = None
            for cmd in attempts:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=FASTSTART_FFMPEG_TIMEOUT_SECONDS)
                if result.returncode == 0:
                    break
                last_err = (result.stderr or "").strip() or f"ffmpeg exited {result.returncode}"
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass
            else:
                raise RuntimeError(last_err or "ffmpeg failed")

        os.chmod(tmp_path, st.st_mode)
        os.utime(tmp_path, (st.st_atime, st.st_mtime))
        os.replace(tmp_path, path)
        log(f"video fixed: {path.name}")
        return True
    except subprocess.TimeoutExpired:
        log(f"fix timed out: {path.name}")
    except Exception as exc:
        log(f"fix failed for {path.name}: {exc}")

    try:
        tmp_path.unlink(missing_ok=True)
    except Exception:
        pass
    return False


def transcode_hevc_to_h264(path: Path) -> bool:
    """Transcode HEVC video to H.264 for browser compatibility."""
    tmp_path = path.with_name(f".{path.stem}.h264{path.suffix}")
    try:
        st = path.stat()
    except FileNotFoundError:
        return False

    try:
        crf = _clamp_int(FASTSTART_X264_CRF, min_value=0, max_value=51)
        base_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-sn",
            "-dn",
            "-c:v",
            "libx264",
            "-preset",
            FASTSTART_X264_PRESET,
            "-crf",
            str(crf),
            "-pix_fmt",
            "yuv420p",
            "-profile:v",
            "high",
            "-movflags",
            "+faststart",
            str(tmp_path),
        ]

        attempts: list[list[str]] = []
        if FASTSTART_COPY_AUDIO:
            attempts.append(base_cmd[:-3] + ["-c:a", "copy"] + base_cmd[-3:])
        attempts.append(base_cmd[:-3] + ["-c:a", "aac", "-b:a", FASTSTART_AAC_BITRATE] + base_cmd[-3:])

        log(f"transcoding HEVC to H.264: {path.name}")

        last_err: str | None = None
        for cmd in attempts:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=FASTSTART_FFMPEG_TIMEOUT_SECONDS)
            if result.returncode == 0:
                break
            last_err = (result.stderr or "").strip() or f"ffmpeg exited {result.returncode}"
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
        else:
            raise RuntimeError(last_err or "ffmpeg failed")

        os.chmod(tmp_path, st.st_mode)
        os.utime(tmp_path, (st.st_atime, st.st_mtime))
        os.replace(tmp_path, path)
        log(f"transcoding complete: {path.name}")
        return True
    except subprocess.TimeoutExpired:
        log(f"transcoding timed out: {path.name}")
    except Exception as exc:
        log(f"transcoding failed for {path.name}: {exc}")

    try:
        tmp_path.unlink(missing_ok=True)
    except Exception:
        pass
    return False


def faststart_in_place(path: Path) -> bool:
    tmp_path = path.with_name(f".{path.stem}.faststart{path.suffix}")
    try:
        st = path.stat()
    except FileNotFoundError:
        return False

    try:
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(path),
            "-map",
            "0",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(tmp_path),
        ]
        subprocess.run(cmd, check=True)

        os.chmod(tmp_path, st.st_mode)
        os.utime(tmp_path, (st.st_atime, st.st_mtime))

        os.replace(tmp_path, path)
        return True
    except Exception as exc:
        log(f"faststart failed for {path.name}: {exc}")
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def main() -> int:
    if len(sys.argv) != 2:
        log("usage: faststart.py <path>")
        return 2

    path = Path(sys.argv[1])

    try:
        if not path.is_file():
            return 0
    except OSError:
        return 0

    if not wait_for_stable_size(path):
        log(f"skipping (file not stable): {path.name}")
        return 0

    path_key = _safe_rel_db_path(path)

    try:
        st = path.stat()
    except Exception:
        st = None

    current_size = int(st.st_size) if st else None
    current_mtime = int(st.st_mtime) if st else None

    # Guard against our own post-processing rename/write events re-triggering the watcher:
    # if we already recorded a completed ("done") run for this exact file version, skip.
    existing = _db_get_video_meta_row(path_key)
    if existing and str(existing.get("status") or "").lower() == "done" and current_size and current_mtime:
        recorded_size, recorded_mtime = _get_recorded_processed_fingerprint(existing)
        if recorded_size and recorded_mtime and recorded_size == current_size and recorded_mtime == current_mtime:
            log(f"skipping (already processed): {path.name}")
            return 0

    uploaded_at = current_mtime or int(time.time())

    original_meta = probe_video_meta(path)
    try:
        original_size = current_size or path.stat().st_size
    except Exception:
        original_size = None

    _db_upsert_video_meta(
        path_key=path_key,
        status="processing",
        action=None,
        error=None,
        uploaded_at=uploaded_at,
        processed_at=None,
        original_size=original_size,
        processed_size=None,
        original_meta=original_meta,
        processed_meta=None,
    )

    try:
        offsets = find_top_level_atom_offsets(path)
    except PermissionError:
        log(f"skipping (permission denied): {path.name}")
        _db_upsert_video_meta(
            path_key=path_key,
            status="error",
            action="none",
            error="permission denied",
            uploaded_at=uploaded_at,
            processed_at=int(time.time()),
            original_size=original_size,
            processed_size=None,
            original_meta=original_meta,
            processed_meta=None,
        )
        return 0
    except Exception as exc:
        log(f"skipping (failed to inspect atoms): {path.name}: {exc}")
        _db_upsert_video_meta(
            path_key=path_key,
            status="error",
            action="none",
            error="failed to inspect atoms",
            uploaded_at=uploaded_at,
            processed_at=int(time.time()),
            original_size=original_size,
            processed_size=None,
            original_meta=original_meta,
            processed_meta=None,
        )
        return 0
    moov_offset = offsets.get("moov")
    mdat_offset = offsets.get("mdat")

    # First check for HEVC and transcode to H.264 for browser compatibility
    codec = get_video_codec(path)
    if codec in ("hevc", "h265"):
        log(f"detected HEVC codec, transcoding to H.264: {path.name}")
        ok = transcode_hevc_to_h264(path)
        processed_meta = probe_video_meta(path) if ok else None
        processed_at = int(time.time())
        processed_size = processed_meta.get("size") if isinstance(processed_meta, dict) else None
        _db_upsert_video_meta(
            path_key=path_key,
            status="done" if ok else "error",
            action="transcode_hevc_to_h264",
            error=None if ok else "transcode failed",
            uploaded_at=uploaded_at,
            processed_at=processed_at,
            original_size=original_size,
            processed_size=processed_size,
            original_meta=original_meta,
            processed_meta=processed_meta,
        )
        return 0  # transcoding already includes faststart

    # Check for extra data streams (iPhone metadata) that cause playback issues
    if has_extra_data_streams(path):
        log(f"detected extra data streams: {path.name}")
        ok = fix_video_errors(path)
        processed_meta = probe_video_meta(path) if ok else None
        processed_at = int(time.time())
        processed_size = processed_meta.get("size") if isinstance(processed_meta, dict) else None
        _db_upsert_video_meta(
            path_key=path_key,
            status="done" if ok else "error",
            action="fix_video_errors_extra_streams",
            error=None if ok else "fix failed",
            uploaded_at=uploaded_at,
            processed_at=processed_at,
            original_size=original_size,
            processed_size=processed_size,
            original_meta=original_meta,
            processed_meta=processed_meta,
        )
        return 0  # re-encoding strips extra streams and includes faststart

    # Check for timestamp errors that cause playback/seeking issues
    if has_timestamp_errors(path):
        log(f"detected timestamp errors: {path.name}")
        ok = fix_video_errors(path)
        processed_meta = probe_video_meta(path) if ok else None
        processed_at = int(time.time())
        processed_size = processed_meta.get("size") if isinstance(processed_meta, dict) else None
        _db_upsert_video_meta(
            path_key=path_key,
            status="done" if ok else "error",
            action="fix_video_errors_timestamp",
            error=None if ok else "fix failed",
            uploaded_at=uploaded_at,
            processed_at=processed_at,
            original_size=original_size,
            processed_size=processed_size,
            original_meta=original_meta,
            processed_meta=processed_meta,
        )
        return 0  # re-encoding includes faststart

    if moov_offset is None or mdat_offset is None:
        _db_upsert_video_meta(
            path_key=path_key,
            status="done",
            action="none",
            error=None,
            uploaded_at=uploaded_at,
            processed_at=uploaded_at,
            original_size=original_size,
            processed_size=original_size,
            original_meta=original_meta,
            processed_meta=original_meta,
        )
        return 0

    if moov_offset < mdat_offset:
        _db_upsert_video_meta(
            path_key=path_key,
            status="done",
            action="already_faststart",
            error=None,
            uploaded_at=uploaded_at,
            processed_at=uploaded_at,
            original_size=original_size,
            processed_size=original_size,
            original_meta=original_meta,
            processed_meta=original_meta,
        )
        return 0

    log(f"optimizing for streaming (moov after mdat): {path.name}")
    ok = faststart_in_place(path)
    if ok:
        log(f"done: {path.name}")

    processed_meta = probe_video_meta(path) if ok else None
    processed_at = int(time.time())
    processed_size = processed_meta.get("size") if isinstance(processed_meta, dict) else None
    _db_upsert_video_meta(
        path_key=path_key,
        status="done" if ok else "error",
        action="faststart",
        error=None if ok else "faststart failed",
        uploaded_at=uploaded_at,
        processed_at=processed_at,
        original_size=original_size,
        processed_size=processed_size,
        original_meta=original_meta,
        processed_meta=processed_meta,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
