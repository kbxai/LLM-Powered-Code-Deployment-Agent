# Step 1: Start with an official Python base image
FROM python:3.11-slim

# Step 2: Install CA certificates + curl (needed for HTTPS requests)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

# Step 3: Set the working directory
WORKDIR /app

# Step 4: Copy requirements and install dependencies
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir --upgrade pip -r requirements.txt

# Step 5: Copy all app files
COPY . .

# Step 6: Expose FastAPI port
EXPOSE 7860

# Step 7: Start FastAPI server
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]
