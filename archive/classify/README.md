# News Article Classifier

LangChain-based classifier for financial news articles using LLM (GPT-4, GPT-4o, or GPT-5).

## Overview

This tool classifies financial news articles based on configurable prompt templates. It's designed to:
- Process large batches of articles efficiently with async/concurrent execution
- Avoid re-classifying articles that have already been processed
- Support different prompts and classification schemas
- Generate labeled training data for fine-tuning custom models

## Features

- **Async Batch Processing**: Multiple batches processed concurrently for speed
- **Deduplication**: Automatically skips articles already classified in previous runs
- **Configurable**: All parameters exposed via CLI arguments
- **Flexible Schema**: Easy to swap prompts/schemas for different classification tasks
- **Progress Tracking**: Visual progress bars using tqdm
- **Timestamped Output**: Each run creates a unique JSONL file with timestamp

## Installation

1. Install dependencies:
```bash
pip install -r classify/requirements.txt
```

2. Set up your OpenAI API key:
```bash
# Add to .env file in project root
echo "OPENAI_API_KEY=your-api-key-here" >> .env
```

## Usage

### Basic Usage

Classify 100 random articles using default settings:
```bash
python classify/classifier.py
```

### Custom Parameters

```bash
python classify/classifier.py \
  --sample-size 500 \
  --batch-size 10 \
  --max-concurrent 20 \
  --model gpt-4o
```

### All Available Options

```
--sample-size       Number of articles to classify (default: 100)
--batch-size        Articles per LLM API call (default: 10)
--max-concurrent    Max parallel API calls (default: 1)
--model             LLM model name (default: gpt-5-chat-latest)
--prompt-file       Path to prompt template (default: classify/prompt1.md)
--input-dir         Article source directory (default: output/alpaca)
--output-dir        Output directory (default: classify/labels)
--temperature       LLM temperature (default: 0.0)
--debug             Print prompts before sending to LLM
```

## Input Format

Articles should be JSON files with the following structure:
```json
{
  "id": 48046348,
  "headline": "Article headline",
  "summary": "Brief summary",
  "content": "Full article content",
  "url": "https://example.com/article",
  "symbols": ["AAPL", "MSFT"],
  "source": "benzinga"
}
```

## Output Format

Classifications are saved as JSONL (one JSON object per line):
```json
{"id": 48046348, "headline": "Article headline", "symbols": ["IBKR"], "type_id": "T4", "type_name": "Market Activity / Sentiment Data"}
```

Output files are saved with timestamps: `classify/labels/labels_YYYYMMDD_HHMMSS.jsonl`

## Classification Schema (T0-T7)

The default schema (`prompt1.md`) classifies financial news into 8 categories:

- **T0**: Background / Informational
- **T1**: Analyst Action
- **T2**: Corporate Event / Announcement  
- **T3**: Financial / Earnings Report
- **T4**: Market Activity / Sentiment Data
- **T5**: Stock Movement Explanation (WIIM)
- **T6**: Macro / Market Recap
- **T7**: Technical / Chart Analysis

See `classify/prompt1.md` for detailed definitions.

## How It Works

1. **Initialization**
   - Loads OpenAI API key from environment
   - Initializes LangChain ChatOpenAI model
   - Loads prompt template from file
   - Scans existing label files to build skip list

2. **Article Loading**
   - Reads all JSON files from input directory
   - Filters out previously classified articles
   - Randomly samples requested number of articles

3. **Batch Processing**
   - Splits articles into batches (based on `--batch-size`)
   - Processes batches concurrently (limited by `--max-concurrent`)
   - Uses asyncio.Semaphore for rate limiting
   - Shows progress with tqdm progress bars

4. **Result Handling**
   - Parses JSONL responses from LLM
   - Enriches results with headline and symbols
   - Saves to timestamped JSONL file
   - Handles errors gracefully with logging

## Using Different Prompts/Schemas

To classify with a different schema:

1. Create a new prompt file (e.g., `classify/prompt2.md`)
2. Run with `--prompt-file` parameter:
```bash
python classify/classifier.py --prompt-file classify/prompt2.md
```

The prompt should:
- Include placeholder `{articles}` where article JSON will be inserted
- Instruct the LLM to output JSONL format
- Define clear classification rules and categories

## Performance Tuning

### Speed Optimization
- Increase `--max-concurrent` (10-50 depending on API rate limits)
- Increase `--batch-size` (5-20 articles per batch)
- Use faster model like `gpt-4o-mini` if acceptable

### Cost Optimization
- Decrease `--max-concurrent` to avoid rate limit charges
- Decrease `--batch-size` for more granular control
- Use `gpt-4o-mini` instead of `gpt-4o`

### Quality Optimization
- Use `--temperature 0.0` for deterministic output (default)
- Use `gpt-4o` or `gpt-4` for best accuracy
- Decrease `--batch-size` for more focused analysis

## Debugging

### Inspecting Prompts

Use the `--debug` flag to see the exact article data being sent to the LLM before each API call:

```bash
python classify/classifier.py --debug --sample-size 3 --batch-size 1
```

This will print:
- The batch number and article IDs
- The complete JSON being inserted into the prompt template
- Useful for verifying prompt formatting and article data

**Example debug output:**
```
DEBUG: Batch with 1 article(s)
Article IDs: [48046348]
ARTICLES JSON TO BE INSERTED:
--------------------------------------------------------------------------------
[
  {
    "id": 48046348,
    "headline": "How Is The Market Feeling About Interactive Brokers Group Inc?",
    "summary": " ",
    "content": "<p><strong>Interactive Brokers...",
    ...
  }
]
```

## Example Runs

**Small test run:**
```bash
python classify/classifier.py --sample-size 10 --batch-size 2
```

**Debug test run to inspect prompts:**
```bash
python classify/classifier.py --debug --sample-size 3 --batch-size 1
```

**Production run:**
```bash
python classify/classifier.py \
  --sample-size 500 \
  --batch-size 10 \
  --max-concurrent 20 \
  --model gpt-4o
```

**Using GPT-4o-mini for faster/cheaper classification:**
```bash
python classify/classifier.py \
  --sample-size 1000 \
  --batch-size 15 \
  --max-concurrent 30 \
  --model gpt-4o-mini
```

## Troubleshooting

**"OPENAI_API_KEY not found in environment"**
- Ensure `.env` file exists in project root with `OPENAI_API_KEY=your-key`
- Or set environment variable: `export OPENAI_API_KEY=your-key`

**Rate limit errors**
- Decrease `--max-concurrent` value
- Add delays between batches (modify code if needed)

**No articles found**
- Check that `output/alpaca/` directory exists and contains JSON files
- Verify articles haven't all been classified already (check `classify/labels/`)

**Out of memory errors**
- Decrease `--sample-size`
- Decrease `--max-concurrent`

## Directory Structure

```
classify/
├── classifier.py          # Main classifier script
├── prompt1.md            # Default classification prompt
├── requirements.txt      # Python dependencies
├── README.md            # This file
└── labels/              # Output directory
    └── *.jsonl          # Timestamped classification results
```

## Notes

- The classifier automatically avoids re-processing articles from previous runs
- Each run creates a new timestamped output file
- Progress is saved incrementally (though currently all at once, could be modified for incremental saves)
- The tool is designed to be run multiple times with different sample sizes
- Classification results can be used as training data for fine-tuning custom models
