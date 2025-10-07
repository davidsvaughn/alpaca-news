# Tiny, fast Python image
FROM python:3.12-slim

WORKDIR /app

# System packages if you need them (optional)
# RUN apt-get update && apt-get install -y --no-install-recommends \
#     build-essential && rm -rf /var/lib/apt/lists/*

# Copy your app code and prompts into the image
COPY app.py /app/app.py
COPY signal_prompt.md /app/signal_prompt.md
COPY type_prompt.md /app/type_prompt.md

# Install dependencies
# fastapi: Web framework
# uvicorn[standard]: ASGI server with standard extras
# python-dotenv: Environment variable management
# pydantic: Data validation
# requests: HTTP client for calling vLLM
# transformers: For token counting with Llama tokenizer
RUN pip install --no-cache-dir \
    fastapi \
    uvicorn[standard] \
    python-dotenv \
    pydantic \
    requests \
    transformers

# Expose the API port (documentary)
EXPOSE 8001

# Start FastAPI
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001"]
