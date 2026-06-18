FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the whole project (app.py, config.py, etc.). .dockerignore keeps data out.
COPY . .

# All data (database, saved files, scheduled files) lives here.
# docker-compose bind-mounts this to a host folder so it stays OUTSIDE the
# container and survives rebuilds.
ENV DATA_DIR=/data
ENV PORT=8894
EXPOSE 8894

CMD ["python", "app.py"]
