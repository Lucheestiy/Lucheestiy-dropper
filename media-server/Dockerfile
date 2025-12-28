FROM python:3.11-slim

WORKDIR /app

# Install dependencies
RUN pip install flask requests gunicorn

COPY app.py /app/

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "4", "--threads", "2", "--access-logfile", "-", "--error-logfile", "-", "app:app"]