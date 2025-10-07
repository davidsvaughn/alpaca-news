#!/usr/bin/env python3
"""
Test script for submitting articles to the vLLM signal classification endpoint.
Uses the signal prompt to evaluate an article's short-term signal potential.
"""

import json
import argparse
import requests
import random
import math
from pathlib import Path
from transformers import AutoTokenizer

FT_PATH = Path(__file__).parent.resolve()
PROMPT_PATH = FT_PATH / "prompts" / "signal_prompt.md"
DATA_PATH = FT_PATH / "data" / "train_data" / "json"


MAX_INPUT_TOKENS = 2048

# Initialize tokenizer for token counting (using Llama 3 tokenizer)
_tokenizer = None

def get_tokenizer():
    """Lazy load and cache the tokenizer."""
    global _tokenizer
    if _tokenizer is None:
        _tokenizer = AutoTokenizer.from_pretrained("meta-llama/Llama-3.2-1B-Instruct")
    return _tokenizer


def count_tokens(text):
    """Count the number of tokens in a text string."""
    tokenizer = get_tokenizer()
    return len(tokenizer.encode(text, add_special_tokens=False))


def truncate_article_content(article, template, max_tokens=MAX_INPUT_TOKENS):
    """
    Truncate the article content field to ensure the full prompt fits within max_tokens.
    
    Args:
        article: The article dictionary to truncate
        template: The prompt template string
        max_tokens: Maximum number of tokens allowed for the full prompt
        
    Returns:
        A tuple of (truncated_article_copy, was_truncated, original_tokens, final_tokens)
    """
    # Make a copy to avoid modifying the original
    article_copy = article.copy()
    
    # Format the full prompt with the original article
    full_prompt = format_prompt(template, article)
    original_tokens = count_tokens(full_prompt)
    
    # If already under limit, return original
    if original_tokens <= max_tokens:
        return article_copy, False, original_tokens, original_tokens
    
    # Calculate how many tokens we need to remove
    tokens_to_remove = original_tokens - max_tokens
    
    # Get the original content
    original_content = article.get("content", "")
    if not original_content:
        # If no content field, we can't truncate further
        return article_copy, False, original_tokens, original_tokens
    
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
        final_prompt = format_prompt(template, article_copy)
        final_tokens = count_tokens(final_prompt)
        return article_copy, True, original_tokens, final_tokens
    
    # Binary search to find the right content length
    # Start by estimating characters per token (roughly 4 chars per token for English)
    estimated_chars = content_token_budget * 4
    
    # Iteratively truncate until we fit within the budget
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
    
    return article_copy, True, original_tokens, final_tokens


# SEED = -1
# # SEED = 4374
# if SEED < 0:
#     SEED = random.randint(0, 10000)
#     print(f"Using random seed: {SEED}")
# random.seed(SEED)


def load_prompt_template(prompt_path=PROMPT_PATH):
    """Load the signal prompt template."""
    with open(prompt_path, 'r') as f:
        return f.read()


def load_article(article_path):
    """Load an article from JSON file."""
    with open(article_path, 'r') as f:
        return json.load(f)


def format_prompt(template, article):
    """Format the prompt template with the article JSON."""
    article_json = json.dumps(article, indent=2)
    return template.replace("{article}", article_json)


def call_vllm_endpoint(prompt, endpoint="http://localhost:8080/v1/chat/completions", 
                       temperature=0.1, max_tokens=3, top_logprobs=0):
    """Send the prompt to the vLLM endpoint and get the response."""
    
    payload = {
        "messages": [
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": temperature,
        "max_tokens": max_tokens
    }
    
    if top_logprobs > 0:
        payload["logprobs"] = True
        payload["top_logprobs"] = top_logprobs
    
    try:
        response = requests.post(endpoint, json=payload, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error calling endpoint: {e}")
        return None


def parse_signal_response(response):
    """Parse the signal strength from the model response."""
    if not response:
        return None
    
    try:
        content = response['choices'][0]['message']['content'].strip()
        
        # Try to parse as JSON first (expected JSONL format)
        try:
            result = json.loads(content)
            # If it's a dict with signal_strength, return it
            if isinstance(result, dict):
                return result
            # If it's just an integer, wrap it in a dict
            elif isinstance(result, int):
                return {"signal_strength": result}
            # sometimes it's already a float, so just round to nearest int
            elif isinstance(result, float):
                return {"signal_strength": round(result)}
        except json.JSONDecodeError:
            # If not valid JSON, try to parse as plain integer
            try:
                signal = int(content)
                return {"signal_strength": signal}
            except ValueError:
                print(f"Could not parse content as JSON or integer: {content}")
                return None
                
    except (KeyError, IndexError) as e:
        print(f"Error accessing response: {e}")
        print(f"Raw response: {response}")
        return None


def calculate_expected_signal(top_logprobs_data, verbose=True):
    """
    Calculate expected signal value from top logprobs.
    
    Args:
        top_logprobs_data: List of logprob items from vLLM response
        
    Returns:
        Expected signal value (0-10) or None if no valid tokens found
    """
    expected_value = 0.0
    total_prob = 0.0
    if verbose:
        probs = [0 for i in range(11)]
                
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
                if verbose:
                    probs[signal_value] = round(probability, 8)
                    
        if verbose:
            # just print 2 cols: token and prob
            print("Token\t| Proba")
            for i in range(11):
                print(f"  {i}\t| {probs[i]}")
            print("-" * 20)
                
    except Exception as e:
        print(f"Error calculating expected signal: {e}")
        return None
    
    if total_prob > 0:
        return round(expected_value, 4)
    else:
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Test signal classification with vLLM endpoint"
    )
    parser.add_argument(
        "--article",
        help="Path to article JSON file (default: example article)"
    )
    parser.add_argument(
        "--endpoint",
        default="http://localhost:8080/v1/chat/completions",
        help="vLLM endpoint URL"
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="Temperature for generation (default: 0.1)"
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=3,
        help="Maximum tokens to generate (default: 3)"
    )
    parser.add_argument(
        "--top-logprobs",
        type=int,
        default=20,
        help="Number of top logprobs to return (0 = disabled, enables expected value calculation)"
    )
    args = parser.parse_args()
    
    #--------------------------------------------------------------------------
    # Set random seed for reproducibility
    #--------------------------------------------------------------------------
    # MAX_INPUT_TOKENS = 1800
    MAX_INPUT_TOKENS = 2000
    
    SEED = -1
    # SEED = 3607
    if SEED < 0:
        SEED = random.randint(0, 10000)
        print(f"Using random seed: {SEED}")
    random.seed(SEED)
    #--------------------------------------------------------------------------
    
    # if no article specified, pick random example
    if not args.article:
        example_files = list(DATA_PATH.glob("*.json"))
        if not example_files:
            print(f"No example articles found in {DATA_PATH}")
            return 1
        # args.article = str(example_files[0])
        args.article = str(random.choice(example_files))
        print(f"No article specified, using example: {args.article}")
    
    # Load prompt template
    print("Loading signal prompt template...")
    template = load_prompt_template()
    
    # Load article
    print(f"Loading article from: {args.article}")
    article = load_article(args.article)
    print(f"Article ID: {article['id']}")
    print(f"Headline: {article['headline']}")
    print(f"Symbol(s): {', '.join(article['symbols'])}")
    print()
    
    # Truncate article content if necessary to fit within token limit
    print("Checking token count and truncating if necessary...")
    article_truncated, was_truncated, original_tokens, final_tokens = truncate_article_content(
        article, template, MAX_INPUT_TOKENS
    )
    
    if was_truncated:
        original_content_len = len(article.get("content", ""))
        truncated_content_len = len(article_truncated.get("content", ""))
        print(f"⚠️  Article content was TRUNCATED:")
        print(f"   Original tokens: {original_tokens}")
        print(f"   Final tokens: {final_tokens}")
        print(f"   Tokens saved: {original_tokens - final_tokens}")
        print(f"   Content length: {original_content_len} -> {truncated_content_len} chars")
        print(f"   Reduction: {100 * (1 - truncated_content_len / original_content_len):.1f}%")
    else:
        print(f"✓ Token count OK: {original_tokens} tokens (limit: {MAX_INPUT_TOKENS})")
    print()
    
    # Format prompt with potentially truncated article
    print("Formatting prompt...")
    prompt = format_prompt(template, article_truncated)
    
    # Call endpoint
    print(f"Calling vLLM endpoint: {args.endpoint}")
    print(f"Temperature: {args.temperature}, Max tokens: {args.max_tokens}")
    if args.top_logprobs > 0:
        print(f"Top logprobs: {args.top_logprobs}")
    response = call_vllm_endpoint(
        prompt, 
        args.endpoint, 
        args.temperature, 
        args.max_tokens,
        args.top_logprobs
    )
    
    if not response:
        print("Failed to get response from endpoint")
        return 1
    
    # Parse result
    print("\n" + "="*60)
    print("RESPONSE:")
    print("="*60)
    result = parse_signal_response(response)
    
    if result:
        print(json.dumps(result, indent=2))
        print()
        print(f"Signal Strength: {result.get('signal_strength', 'N/A')}/10")
        
        # Calculate expected value from logprobs if available
        if args.top_logprobs > 0:
            try:
                top_logprobs_data = response["choices"][0]["logprobs"]["content"][0]["top_logprobs"]
                expected_signal = calculate_expected_signal(top_logprobs_data)
                
                if expected_signal is not None:
                    print(f"Expected Signal Value: {expected_signal}/10")
                else:
                    print("Expected Signal Value: Could not calculate (no valid signal tokens found)")
            except (KeyError, IndexError, TypeError) as e:
                print(f"Warning: Could not extract logprobs data: {e}")
    else:
        print("Failed to parse response")
        return 1
    
    return 0

def test_token_ten():
    from transformers import AutoTokenizer
    # Load the tokenizer for a Llama 3 model
    tokenizer = AutoTokenizer.from_pretrained("meta-llama/Llama-3.2-1B-Instruct")
    for i in range(11):
        encoded = tokenizer.encode(str(i), add_special_tokens=False)
        decoded_tokens = [tokenizer.decode([token], skip_special_tokens=True) for token in encoded]
        print(f"Token ID for {i}: {encoded}")
        print(f"Decoded tokens for {i}: {decoded_tokens}")



if __name__ == "__main__":
    # test_token_ten()
    
    main()
    
    # while True:
    #     if main() != 0:
    #         break
