# FinScore API

FastAPI service for scoring financial news articles using a fine-tuned vLLM model (`davidsvaughn/finscore-W4A16`).

## Overview

This Docker Compose setup provides:
- **vLLM service**: Serves the fine-tuned model with GPU acceleration
- **FastAPI service**: Provides REST API endpoints for scoring articles
- **Web interface (optional)**: Flask-based UI for testing the API with a browser

## Quick Start

1. **Download the model:**
    ```bash
    hf download davidsvaughn/finscore-W4A16 --local-dir ~/models/finscore-W4A16
    ```

2. **Start the services:**
   ```bash
   cd finscore-api
   docker compose up -d
   ```

   **Or start with the web interface:**
   ```bash
   docker compose --profile web up -d
   ```

3. **Check service health:**
   ```bash
   curl http://localhost:8001/health
   ```

4. **Score an article:**

   **Option A: Using the web interface** (if started with `--profile web`):
   - Open your browser to http://localhost:5000
   - Enter the article folder path (default: `output/train_data/json`)
   - Click "Scan Folder" to load available JSON files
   - Select an article from the list
   - Click "Submit to API" to see the score

   **Option B: Using curl:**
   ```bash
   curl -X POST http://localhost:8001/score \
     -H "Content-Type: application/json" \
     -d @article.json
   ```

## API Endpoints

### POST /score

Score a financial news article for short-term signal potential (0-10 scale).

**Request Body:**
```json
{
  "id": 48046348,
  "headline": "Company announces major partnership",
  "author": "Jane Doe",
  "created_at": "2025-01-07T10:00:00Z",
  "updated_at": "2025-01-07T10:00:00Z",
  "summary": "Brief summary of the article",
  "content": "Full article content...",
  "url": "https://example.com/article",
  "images": [],
  "symbols": ["AAPL", "MSFT"],
  "source": "Reuters"
}
```

**Response:**
```json
{
  "article_id": 48046348,
  "signal": 6.8234,
  "truncated": 0
}
```

Where:
- `signal`: Expected signal score (0-10) calculated from logprobs, or integer if logprobs unavailable
- `truncated`: Number of tokens truncated from content (0 if none)

### POST /type

Classify a financial news article into one of 8 types (0-7).

**Request Body:** (Same format as `/score`)
```json
{
  "id": 48046348,
  "headline": "Company announces major partnership",
  "author": "Jane Doe",
  "created_at": "2025-01-07T10:00:00Z",
  "updated_at": "2025-01-07T10:00:00Z",
  "summary": "Brief summary of the article",
  "content": "Full article content...",
  "url": "https://example.com/article",
  "images": [],
  "symbols": ["AAPL", "MSFT"],
  "source": "Reuters"
}
```

**Response:**
```json
{
  "article_id": 48046348,
  "type": 2,
  "truncated": 0
}
```

Where:
- `type`: Article classification (0-7):
  - **0**: Background / Informational ("Fluff") - evergreen/retrospective/educational content
  - **1**: Analyst Action - brokerage ratings, price targets, coverage changes
  - **2**: Corporate Event / Announcement - M&A, partnerships, product launches, leadership changes
  - **3**: Financial / Earnings Report - revenue, EPS, guidance, results
  - **4**: Market Activity / Sentiment Data - options activity, short interest, insider trades
  - **5**: Stock Movement Explanation (WIIM) - "why is it moving" post-hoc explanations
  - **6**: Macro / Market Recap - index/sector roundups, market-wide commentary
  - **7**: Technical / Chart Analysis - chart patterns, technical indicators
- `truncated`: Number of tokens truncated from content (0 if none)

### GET /health

Health check endpoint.

**Response:**
```json
{
  "status": "healthy"
}
```

## Web Interface (Optional)

The web interface provides an easy way to test the API without writing code or using curl. It's especially useful for:
- Testing the API with your own article files
- Viewing formatted API responses
- Exploring article collections with pagination and search

### Starting with Web Interface

To include the web interface, use the `--profile web` flag:

```bash
# Start all services including web interface
docker compose --profile web up -d

# Or without web interface (default)
docker compose up -d
```

The web interface will be available at: http://localhost:5000

### Features

1. **Smart Folder Loading**: 
   - Handles folders with 20,000+ JSON files efficiently
   - Loads files in batches of 100 with pagination
   - Includes search/filter capability
   - Shows warnings for large directories

2. **Article Display**:
   - View full JSON content of selected articles
   - Syntax-highlighted display
   - Easy file selection from list

3. **API Testing**:
   - Submit articles to the FastAPI endpoint
   - View formatted responses with signal scores
   - See token truncation information
   - Display error messages clearly

4. **Configuration**:
   - Default folder: `output/train_data/json`
   - Easily change folder path via UI
   - Search files by name
   - Load more files on demand

### Web Interface Environment Variables

Configure the web service in `compose.yml`:

- `API_URL`: FastAPI endpoint (default: `http://api:8001/score`)
- `DEFAULT_FOLDER`: Default article folder path (default: `output/train_data/json`)

### Accessing Article Files

The web interface mounts the parent directory to `/project` inside the container. You can specify any folder path starting with `/project/`:

- `/project/output/train_data/json` (default)
- `/project/cline/ex/articles`
- `/project/any/other/path/with/json/files`

Or use relative paths within the web UI (they will be resolved relative to `/project`).

## Configuration

### Environment Variables

Configure the API service via environment variables in `compose.yml`:

- `VLLM_URL`: vLLM endpoint URL (default: `http://vllm:8000/v1/chat/completions`)
- `PROMPT_FILE`: Path to prompt template (default: `/app/signal_prompt.md`)
- `MAX_TOKENS`: Max tokens to generate (default: `2`)
- `TEMPERATURE`: Sampling temperature (default: `0`)
- `TOP_LOGPROBS`: Number of top logprobs (default: `20`)
- `TIMEOUT`: Request timeout in seconds (default: `30`)
- `MAX_INPUT_TOKENS`: Max input tokens before truncation (default: `2000`)
- `API_KEY`: Optional API key for authentication (commented out by default)

### Model Storage Options

**Option 1: HuggingFace Cache (default)**

The model is downloaded from HuggingFace and cached locally:

```yaml
volumes:
  - ~/.cache/huggingface:/root/.cache/huggingface
environment:
  HUGGING_FACE_HUB_TOKEN: ${HUG_READ_TOKEN}
```

**Option 2: Local Model Path**

To use a locally stored model, edit `compose.yml`:

1. Comment out the HuggingFace volume and token
2. Uncomment the local model volume
3. Update the model path in the command

```yaml
command:
  - --model
  # - davidsvaughn/finscore-W4A16
  - /models/finscore-W4A16  # Uncomment this line
volumes:
  # - ~/.cache/huggingface:/root/.cache/huggingface
  - /home/david/models:/models  # Uncomment this line
```

### GPU Configuration

The vLLM service is configured for optimal GPU usage:

```yaml
deploy:
  resources:
    reservations:
      devices:
        - capabilities: ["gpu"]
```

Model parameters:
- `--max-num-seqs 1`: Process one sequence at a time
- `--max-model-len 2048`: Maximum model context length
- `--max-num-batched-tokens 32`: Batch size limit
- `--gpu-memory-utilization 0.2`: Use 20% of GPU memory

## Authentication (Optional)

To enable API key authentication:

1. Edit `compose.yml` and uncomment the `API_KEY` line:
   ```yaml
   environment:
     API_KEY: your-secret-key-here
   ```

2. Include the API key in requests:
   ```bash
   curl -X POST http://localhost:8001/score \
     -H "Content-Type: application/json" \
     -H "Authorization: Bearer your-secret-key-here" \
     -d @article.json
   ```

## Token Truncation

The API automatically truncates article content if the formatted prompt exceeds `MAX_INPUT_TOKENS` (default: 2000). The response includes the number of tokens truncated:

```json
{
  "article_id": 48046348,
  "signal": 7.2,
  "truncated": 150
}
```

## Monitoring

View logs:
```bash
docker compose logs -f api
docker compose logs -f vllm
```

Stop services:
```bash  
docker compose down
```

## Development & Rebuilding

When you make changes to containers (code in `api/` or `web/` directories), use these commands to rebuild:

### Quick Rebuild Commands

**Rebuild and restart just the API service:**
```bash
docker compose up -d --build api
```
This rebuilds the API image and restarts only that container, leaving vLLM running.

**Rebuild and restart the web interface:**
```bash
docker compose --profile web up -d --build web
```

**Just rebuild an image (without restarting):**
```bash
docker compose build api
# or
docker compose build web
```
Then separately restart it:
```bash
docker compose up -d api
# or
docker compose --profile web up -d web
```

**Force a complete rebuild (no cache):**
```bash
docker compose build --no-cache api
docker compose up -d api
# or for web
docker compose build --no-cache web
docker compose --profile web up -d web
```

### What Changes Require Rebuilding?

**API Service:**
- **Requires rebuild:** Changes to `api/Dockerfile`, `api/app.py`, `api/signal_prompt.md`, or any files in the `api/` directory
- **No rebuild needed:** Changes to environment variables in `compose.yml` (just restart: `docker compose restart api`)

**Web Service:**
- **Requires rebuild:** Changes to `web/Dockerfile`, `web/app.py`, `web/templates/`, or any files in the `web/` directory
- **No rebuild needed:** Changes to environment variables in `compose.yml` (just restart: `docker compose restart web`)

### Additional Development Commands

**View real-time logs:**
```bash
docker compose logs -f api
docker compose logs -f vllm
docker compose logs -f web  # if using web interface
```

**Check service status:**
```bash
docker compose ps
```

**Restart a service:**
```bash
docker compose restart api
```

**Stop all services:**
```bash
docker compose down
```

**Start all services:**
```bash
docker compose up -d
```

The API service is configured with `restart: unless-stopped`, so it will automatically restart if it crashes.

## Testing

Test with an example article from the project:
```bash
curl -X POST http://localhost:8001/score \
  -H "Content-Type: application/json" \
  -d @../output/alpaca/2025-10-06T15-01-02Z_48046348.json
```

Or use the test script:
```bash
# After starting services with docker compose
cd ..
python ft/test_signal.py --endpoint http://localhost:8001/score
```

## Architecture

### Without Web Interface (default)
```
┌─────────────┐      ┌──────────────┐      ┌─────────────┐
│   Client    │─────▶│  FastAPI     │─────▶│    vLLM     │
│             │      │  (port 8001) │      │ (internal)  │
│             │◀─────│              │◀─────│             │
└─────────────┘      └──────────────┘      └─────────────┘
                           │
                           ├─ Token truncation
                           ├─ Prompt formatting
                           └─ Logprobs calculation
```

### With Web Interface (`--profile web`)
```
┌─────────────┐      ┌──────────────┐      ┌──────────────┐      ┌─────────────┐
│   Browser   │─────▶│    Flask     │─────▶│  FastAPI     │─────▶│    vLLM     │
│ (port 5000) │      │    (web)     │      │  (port 8001) │      │ (internal)  │
│             │◀─────│              │◀─────│              │◀─────│             │
└─────────────┘      └──────────────┘      └──────────────┘      └─────────────┘
                           │                      │
                           │                      ├─ Token truncation
                           ├─ Load JSON files     ├─ Prompt formatting
                           ├─ Pagination          └─ Logprobs calculation
                           └─ Search/filter
```

## Troubleshooting

**Service won't start:**
- Ensure Docker and Docker Compose are installed
- Check GPU availability: `nvidia-smi`
- Verify HuggingFace token is set: `echo $HUG_READ_TOKEN`

**Model download fails:**
- Check internet connection
- Verify HuggingFace token has read permissions
- Try pulling model manually: `huggingface-cli download davidsvaughn/finscore-W4A16`

**API returns 503 Service Unavailable:**
- Wait for vLLM to fully start (check logs: `docker compose logs vllm`)
- Model loading can take several minutes on first start

**Truncation warnings:**
- Increase `MAX_INPUT_TOKENS` if needed
- Or reduce article content before submission
