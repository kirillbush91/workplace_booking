FROM mcr.microsoft.com/playwright/python:v1.51.0-jammy

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY booking_bot /app/booking_bot

CMD ["python", "-m", "booking_bot"]

