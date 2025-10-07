#!/usr/bin/env python3
"""
FastAPI service for financial news signal scoring using a fine-tuned vLLM model.
"""

import json
import math
import os
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

import requests
from fastapi import FastAPI, HTTPException, Security, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from transformers import AutoTokenizer

# ============================================================================
# Configuration from environment variables
# ============================================================================
VLLM_URL = os.getenv("VLLM_URL", "http://vllm:8000/v1/chat/completions")
PROMPT_FILE = os.getenv("PROMPT_FILE", "/app/signal_prompt.md")
MODEL_PATH = os.getenv("MODEL_PATH", None)
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8001"))
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "2"))
TEMPERATURE = float(os.getenv("TEMPERATURE", "0"))
TOP_LOGPROBS = int(os.getenv("TOP_LOGPROBS", "20"))
TIMEOUT = int(os.getenv("TIMEOUT", "30"))
MAX_INPUT_TOKENS = int(os.getenv("MAX_INPUT_TOKENS", "2000"))
API_KEY = os.getenv("API_KEY", None)  # Optional API key

# ============================================================================
# Global state
# ============================================================================
_tokenizer = None
_prompt_template = None

# ============================================================================
# FastAPI app setup
# ============================================================================
app = FastAPI(
    title="FinScore Signal API",
    description="API for scoring financial news articles using a fine-tuned model",
    version="1.0.0"
)

# Optional security
security = HTTPBearer(auto_error=False) if API_KEY else None


def verify_api_key(credentials: Optional[HTTPAuthorizationCredentials] = Security(security)):
    """Verify API key if authentication is enabled."""
    if API_KEY:
        if not credentials or credentials.credentials != API_KEY:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key"
            )


# ============================================================================
# Pydantic models
# ============================================================================
class ScoreRequest(BaseModel):
    """Request model for scoring an article."""
    id: int = Field(..., description="Article ID")
    headline: str = Field(..., description="Article headline")
    author: str = Field(..., description="Article author")
    created_at: str = Field(..., description="Creation timestamp")
    updated_at: str = Field(..., description="Update timestamp")
    summary: str = Field(..., description="Article summary")
    content: str = Field(..., description="Article content")
    url: Optional[str] = Field(None, description="Article URL")
    images: Optional[list] = Field(None, description="Article images")
    symbols: list = Field(default_factory=list, description="Related stock symbols")
    source: Optional[str] = Field(None, description="Article source")


class ScoreResponse(BaseModel):
    """Response model for scoring result."""
    article_id: int = Field(..., description="Article ID")
    signal: float = Field(..., description="Signal score (0-10)")
    truncated: int = Field(..., description="Number of tokens truncated (0 if none)")


class HealthResponse(BaseModel):
    """Health check response."""
    status: str = Field(..., description="Service status")


# ============================================================================
# Utility functions
# ============================================================================
def get_tokenizer():
    """Lazy load and cache the tokenizer."""
    global _tokenizer
    if _tokenizer is None:
        _tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH)
    return _tokenizer


def count_tokens(text: str) -> int:
    """Count the number of tokens in a text string."""
    tokenizer = get_tokenizer()
    return len(tokenizer.encode(text, add_special_tokens=False))


def load_prompt_template() -> str:
    """Load the signal prompt template from file."""
    global _prompt_template
    if _prompt_template is None:
        with open(PROMPT_FILE, 'r') as f:
            _prompt_template = f.read()
    return _prompt_template


def format_prompt(template: str, article: Dict[str, Any]) -> str:
    """Format the prompt template with the article JSON."""
    article_json = json.dumps(article, indent=2)
    return template.replace("{article}", article_json)


def truncate_article_content(
    article: Dict[str, Any],
    template: str,
    max_tokens: int = MAX_INPUT_TOKENS
) -> Tuple[Dict[str, Any], int]:
    """
    Truncate the article content field to ensure the full prompt fits within max_tokens.
    
    Returns:
        A tuple of (truncated_article, tokens_truncated)
    """
    # Make a copy to avoid modifying the original
    article_copy = article.copy()
    
    # Format the full prompt with the original article
    full_prompt = format_prompt(template, article)
    original_tokens = count_tokens(full_prompt)
    
    # If already under limit, return original
    if original_tokens <= max_tokens:
        return article_copy, 0
    
    # Calculate how many tokens we need to remove
    tokens_to_remove = original_tokens - max_tokens
    
    # Get the original content
    original_content = article.get("content", "")
    if not original_content:
        # If no content field, we can't truncate further
        return article_copy, 0
    
    # Count tokens in the article JSON without content
    article_without_content = article.copy()
    article_without_content["content"] = ""
    prompt_without_content = format_prompt(template, article_without_content)
    base_tokens = count_tokens(prompt_without_content)
    
    # Calculate how many tokens we have available for content
    content_token_budget = max_tokens - base_tokens
    
    if content_token_budget <= 0:
        # Even without content, we exceed the limit - just empty the content
        article_copy["content"] = ""
        return article_copy, original_tokens - count_tokens(format_prompt(template, article_copy))
    
    # Binary search to find the right content length
    low, high = 0, len(original_content)
    best_length = 0
    
    while low <= high:
        mid = (low + high) // 2
        article_copy["content"] = original_content[:mid]
        test_prompt = format_prompt(template, article_copy)
        test_tokens = count_tokens(test_prompt)
        
        if test_tokens <= max_tokens:
            best_length = mid
            low = mid + 1
        else:
            high = mid - 1
    
    # Apply the best truncation
    article_copy["content"] = original_content[:best_length]
    final_prompt = format_prompt(template, article_copy)
    final_tokens = count_tokens(final_prompt)
    
    return article_copy, original_tokens - final_tokens


def call_vllm_endpoint(prompt: str) -> Optional[Dict[str, Any]]:
    """Send the prompt to the vLLM endpoint and get the response."""
    payload = {
        "messages": [
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "logprobs": True,
        "top_logprobs": TOP_LOGPROBS
    }
    
    try:
        response = requests.post(VLLM_URL, json=payload, timeout=TIMEOUT)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Error calling vLLM endpoint: {str(e)}"
        )


def parse_signal_response(response: Dict[str, Any]) -> Optional[int]:
    """Parse the signal strength from the model response."""
    try:
        content = response['choices'][0]['message']['content'].strip()
        
        # Try to parse as JSON first (expected JSONL format)
        try:
            result = json.loads(content)
            # If it's a dict with signal_strength, return it
            if isinstance(result, dict) and "signal_strength" in result:
                return result["signal_strength"]
            # If it's just an integer, return it
            elif isinstance(result, int):
                return result
            # If it's a float, round to nearest int
            elif isinstance(result, float):
                return round(result)
        except json.JSONDecodeError:
            # If not valid JSON, try to parse as plain integer
            try:
                return int(content)
            except ValueError:
                pass
                
    except (KeyError, IndexError) as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error parsing model response: {str(e)}"
        )
    
    return None


def calculate_expected_signal(top_logprobs_data: list) -> Optional[float]:
    """
    Calculate expected signal value from top logprobs.
    
    Args:
        top_logprobs_data: List of logprob items from vLLM response
        
    Returns:
        Expected signal value (0-10) or None if no valid tokens found
    """
    expected_value = 0.0
    total_prob = 0.0
    
    try:
        for item in top_logprobs_data:
            token = item.get("token", "").strip()
            
            # Check if token is a valid signal value (0-10)
            if token in ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10"]:
                signal_value = int(token)
                logprob = item.get("logprob", 0)
                probability = math.exp(logprob)
                expected_value += signal_value * probability
                total_prob += probability
                
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error calculating expected signal: {str(e)}"
        )
    
    if total_prob > 0:
        return round(expected_value, 4)
    else:
        return None


# ============================================================================
# API endpoints
# ============================================================================
@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}


@app.post("/score", response_model=ScoreResponse, dependencies=[Security(verify_api_key)] if API_KEY else [])
async def score_article(request: ScoreRequest):
    """
    Score a financial news article for short-term signal potential.
    
    Args:
        request: Article data to score
        
    Returns:
        ScoreResponse with signal score and truncation info
    """
    # Convert request to dict
    article = request.model_dump()
    
    # Load prompt template
    template = load_prompt_template()
    
    # Truncate article content if necessary
    article_truncated, tokens_truncated = truncate_article_content(
        article, template, MAX_INPUT_TOKENS
    )
    
    # Format prompt
    prompt = format_prompt(template, article_truncated)
    
    # Call vLLM endpoint
    response = call_vllm_endpoint(prompt)
    
    # Parse signal strength
    signal_strength = parse_signal_response(response)
    
    # Calculate expected signal from logprobs
    signal_value = signal_strength  # Default to parsed value
    
    try:
        top_logprobs_data = response["choices"][0]["logprobs"]["content"][0]["top_logprobs"]
        expected_signal = calculate_expected_signal(top_logprobs_data)
        
        if expected_signal is not None:
            signal_value = expected_signal
        elif signal_strength is not None:
            signal_value = float(signal_strength)
    except (KeyError, IndexError, TypeError):
        # If we can't get logprobs, use the parsed signal strength
        if signal_strength is not None:
            signal_value = float(signal_strength)
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Could not extract signal value from response"
            )
    
    return ScoreResponse(
        article_id=article["id"],
        signal=signal_value,
        truncated=tokens_truncated
    )


# ============================================================================
# Main entry point (for local testing)
# ============================================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
