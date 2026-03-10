#!/usr/bin/env python3
"""
News Article Classifier using LangChain and LLM
Classifies financial news articles based on a configurable prompt/schema
"""

import argparse
import asyncio
import json
import os
import random
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Set, Any
from dataclasses import dataclass

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from tqdm.asyncio import tqdm_asyncio
import aiofiles


@dataclass
class ClassifierConfig:
    """Configuration for the classifier"""
    sample_size: int
    batch_size: int
    max_concurrent: int
    model: str
    prompt_file: str
    input_dir: str
    output_dir: str
    input_filter: str = None
    temperature: float = 0.0
    debug: bool = False


class ArticleClassifier:
    """Async article classifier using LangChain"""
    
    def __init__(self, config: ClassifierConfig):
        self.config = config
        self.llm = None
        self.prompt_template = None
        self.classified_ids: Set[int] = set()
        
    def initialize(self):
        """Initialize LangChain components and load existing classifications"""
        # Load API key
        load_dotenv()
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY not found in environment")
        
        # Initialize LLM
        self.llm = ChatOpenAI(
            model=self.config.model,
            temperature=self.config.temperature,
            api_key=api_key
        )
        
        # Load prompt template
        self.prompt_template = self._load_prompt_template()
        
        # Load existing classifications
        self.classified_ids = self._load_classified_ids()
        print(f"Found {len(self.classified_ids)} previously classified articles")
    
    def _load_prompt_template(self) -> ChatPromptTemplate:
        """Load and create prompt template from file"""
        prompt_path = Path(self.config.prompt_file)
        if not prompt_path.exists():
            raise FileNotFoundError(f"Prompt file not found: {prompt_path}")
        
        with open(prompt_path, 'r', encoding='utf-8') as f:
            prompt_text = f.read()
        
        # Create template with articles placeholder
        return ChatPromptTemplate.from_messages([
            ("system", prompt_text)
        ])
    
    def _load_classified_ids(self) -> Set[int]:
        """Load all previously classified article IDs"""
        classified_ids = set()
        labels_dir = Path(self.config.output_dir)
        
        if not labels_dir.exists():
            labels_dir.mkdir(parents=True, exist_ok=True)
            return classified_ids
        
        for jsonl_file in labels_dir.glob("*.jsonl"):
            try:
                with open(jsonl_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        if line.strip():
                            data = json.loads(line)
                            classified_ids.add(data['id'])
            except Exception as e:
                print(f"Warning: Error reading {jsonl_file}: {e}")
        
        return classified_ids
    
    def _load_filter_ids(self) -> Set[int]:
        """Load article IDs from input filter directory"""
        if not self.config.input_filter:
            return None
        
        filter_ids = set()
        filter_dir = Path(self.config.input_filter)
        
        if not filter_dir.exists():
            raise FileNotFoundError(f"Input filter directory not found: {filter_dir}")
        
        for jsonl_file in filter_dir.glob("*.jsonl"):
            try:
                with open(jsonl_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        if line.strip():
                            data = json.loads(line)
                            filter_ids.add(data['id'])
            except Exception as e:
                print(f"Warning: Error reading {jsonl_file}: {e}")
        
        print(f"Loaded {len(filter_ids)} article IDs from input filter: {filter_dir}")
        return filter_ids
    
    def load_articles(self) -> List[Dict[str, Any]]:
        """Load and filter articles, then sample randomly"""
        input_dir = Path(self.config.input_dir)
        if not input_dir.exists():
            raise FileNotFoundError(f"Input directory not found: {input_dir}")
        
        # Load filter IDs if input_filter is specified
        filter_ids = self._load_filter_ids()
        
        # Load all articles
        all_articles = []
        print(f"Loading articles from {input_dir}...")
        
        for json_file in input_dir.glob("*.json"):
            try:
                with open(json_file, 'r', encoding='utf-8') as f:
                    article = json.load(f)
                    
                    # Apply input filter first (if specified)
                    if filter_ids is not None and article['id'] not in filter_ids:
                        continue
                    
                    # Filter out already classified
                    if article['id'] not in self.classified_ids:
                        all_articles.append(article)
            except Exception as e:
                print(f"Warning: Error reading {json_file}: {e}")
        
        if filter_ids is not None:
            print(f"After applying input filter: {len(all_articles)} articles remain")
        print(f"Found {len(all_articles)} unclassified articles")
        
        # Random sample
        sample_size = min(self.config.sample_size, len(all_articles))
        if sample_size < len(all_articles):
            sampled_articles = random.sample(all_articles, sample_size)
            print(f"Randomly sampled {sample_size} articles for classification")
        else:
            sampled_articles = all_articles
            print(f"Using all {sample_size} available articles")
        
        return sampled_articles
    
    def _format_article_for_prompt(self, article: Dict[str, Any]) -> Dict[str, Any]:
        """Format article for prompt input, keeping only relevant fields"""
        return {
            'id': article['id'],
            'headline': article['headline'],
            'summary': article.get('summary', ''),
            'content': article.get('content', ''),
            'url': article.get('url', ''),
            'symbols': article.get('symbols', []),
            'source': article.get('source', '')
        }
    
    def _create_batch_prompt(self, articles: List[Dict[str, Any]]) -> str:
        """Create prompt for a batch of articles"""
        formatted_articles = [self._format_article_for_prompt(a) for a in articles]
        articles_json = json.dumps(formatted_articles, indent=2)
        return articles_json
    
    def _strip_markdown_codeblocks(self, text: str) -> str:
        """Strip markdown code block markers from LLM response
        
        Handles responses wrapped in:
        - ```json ... ```
        - ``` ... ```
        - Plain text (no change)
        
        Removes any lines that are just markdown code block markers.
        Returns the content without markdown markers.
        """
        import re
        
        lines = text.strip().split('\n')
        
        # Filter out lines that are markdown code block markers
        # Pattern matches: ``` optionally followed by a language name (only alphanumeric chars)
        # This ensures we only remove marker lines, not content that might contain backticks
        marker_pattern = re.compile(r'^\s*```[a-zA-Z]*\s*$')
        
        filtered_lines = []
        for line in lines:
            # Skip lines that match the markdown code block pattern
            if marker_pattern.match(line):
                continue
            filtered_lines.append(line)
        
        return '\n'.join(filtered_lines)
    
    async def _classify_batch(self, batch: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Classify a batch of articles asynchronously"""
        try:
            # Format articles for prompt
            articles_json = self._create_batch_prompt(batch)
            
            # Debug: Print full formatted prompt before sending
            if self.config.debug:
                print("\n" + "=" * 80)
                print(f"DEBUG: Batch with {len(batch)} article(s)")
                print("Article IDs:", [a['id'] for a in batch])
                print("=" * 80)
                print("FULL PROMPT SENT TO LLM:")
                print("-" * 80)
                try:
                    # Format the complete prompt to see what the LLM actually receives
                    formatted_messages = self.prompt_template.format_messages(articles=articles_json)
                    for i, msg in enumerate(formatted_messages):
                        print(f"\n--- Message {i+1} ({msg.type}) ---")
                        print(msg.content)
                except Exception as e:
                    print(f"Error formatting messages: {e}")
                    print(f"Full exception: {type(e).__name__}: {str(e)}")
                    import traceback
                    traceback.print_exc()
                    # Fallback: show raw template and articles
                    print("\nFallback - Raw Template:")
                    print(self.prompt_template.messages[0].prompt.template)
                    print("\nFallback - Articles JSON:")
                    print(articles_json)
                print("\n" + "=" * 80)
                print()
            
            # Create chain
            chain = self.prompt_template | self.llm | StrOutputParser()
            
            # Invoke LLM
            response = await chain.ainvoke({
                "articles": articles_json
            })
            
            # Strip markdown code blocks if present
            cleaned_response = self._strip_markdown_codeblocks(response)
            
            # Parse JSONL response
            results = []
            for line in cleaned_response.strip().split('\n'):
                if line.strip():
                    try:
                        result = json.loads(line)
                        # Find matching article to get headline and symbols
                        for article in batch:
                            if article['id'] == result['id']:
                                result['headline'] = article['headline']
                                result['symbols'] = article.get('symbols', [])
                                break
                        results.append(result)
                    except json.JSONDecodeError as e:
                        print(f"Warning: Failed to parse line: {line[:100]}... Error: {e}")
            
            return results
            
        except Exception as e:
            print(f"Error classifying batch: {e}")
            return []
    
    async def classify_articles(self, articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Classify all articles with concurrent batch processing"""
        # Split into batches
        batches = []
        for i in range(0, len(articles), self.config.batch_size):
            batch = articles[i:i + self.config.batch_size]
            batches.append(batch)
        
        print(f"\nProcessing {len(articles)} articles in {len(batches)} batches")
        print(f"Batch size: {self.config.batch_size}, Max concurrent: {self.config.max_concurrent}")
        
        # Process batches concurrently with semaphore
        semaphore = asyncio.Semaphore(self.config.max_concurrent)
        
        async def process_with_semaphore(batch):
            async with semaphore:
                return await self._classify_batch(batch)
        
        # Run all batches with progress bar
        tasks = [process_with_semaphore(batch) for batch in batches]
        batch_results = await tqdm_asyncio.gather(*tasks, desc="Classifying batches")
        
        # Flatten results
        all_results = []
        for batch_result in batch_results:
            all_results.extend(batch_result)
        
        return all_results
    
    async def save_results(self, results: List[Dict[str, Any]]):
        """Save classification results to timestamped JSONL file"""
        # Create output directory if needed
        output_dir = Path(self.config.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # Create timestamped filename
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = output_dir / f"labels_{timestamp}.jsonl"
        
        # Save results
        print(f"\nSaving {len(results)} classifications to {output_file}")
        async with aiofiles.open(output_file, 'w', encoding='utf-8') as f:
            for result in results:
                await f.write(json.dumps(result) + '\n')
        
        print(f"Successfully saved results to {output_file}")
        return output_file


async def main():
    """Main execution function"""
    parser = argparse.ArgumentParser(
        description="Classify financial news articles using LLM"
    )
    parser.add_argument(
        '--sample-size',
        type=int,
        default=1000,
        help='Number of articles to classify (default: 1000)'
    )
    parser.add_argument(
        '--batch-size',
        type=int,
        default=10,
        help='Number of articles per LLM call (default: 10)'
    )
    parser.add_argument(
        '--max-concurrent',
        type=int,
        default=10,
        help='Maximum number of concurrent API calls (default: 10)'
    )
    parser.add_argument(
        '--model',
        type=str,
        default='gpt-5-chat-latest',
        help='LLM model to use (default: gpt-5-chat-latest)'
    )
    parser.add_argument(
        '--input-dir',
        type=str,
        default='output/alpaca',
        help='Directory containing article JSON files (default: output/alpaca)'
    )
    parser.add_argument(
        '--classify-dir',
        default='classify/signal',
        type=str,
        help='Base classification directory (e.g., classify/type). Will automatically use prompt.md and labels/ from this directory'
    )
    parser.add_argument(
        '--prompt-file',
        type=str,
        help='Path to prompt template file (overrides --classify-dir if both provided)'
    )
    parser.add_argument(
        '--output-dir',
        type=str,
        help='Directory to save classification results (overrides --classify-dir if both provided)'
    )
    parser.add_argument(
        '--input-filter',
        # default='classify/type/labels/',
        type=str,
        help='Path to labels directory to filter input articles (only process articles with IDs found in this directory)'
    )
    parser.add_argument(
        '--temperature',
        type=float,
        default=0.0,
        help='LLM temperature (default: 0.0 for deterministic output)'
    )
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Print prompts before sending to LLM for inspection'
    )
    
    args = parser.parse_args()
    
    # Determine prompt_file and output_dir based on classify_dir or explicit args
    if args.classify_dir:
        classify_path = Path(args.classify_dir)
        prompt_file = args.prompt_file or str(classify_path / 'prompt.md')
        output_dir = args.output_dir or str(classify_path / 'labels')
    else:
        # Fallback to defaults if neither classify_dir nor explicit paths provided
        prompt_file = args.prompt_file or 'classify/type/prompt.md'
        output_dir = args.output_dir or 'classify/type/labels'
    
    # Create config
    config = ClassifierConfig(
        sample_size=args.sample_size,
        batch_size=args.batch_size,
        max_concurrent=args.max_concurrent,
        model=args.model,
        prompt_file=prompt_file,
        input_dir=args.input_dir,
        output_dir=output_dir,
        input_filter=args.input_filter,
        temperature=args.temperature,
        debug=args.debug
        # debug=True  # Always enable debug for now (can be toggled later
    )
    
    # Run classifier
    print("=" * 80)
    print("News Article Classifier")
    print("=" * 80)
    print(f"Model: {config.model}")
    print(f"Sample size: {config.sample_size}")
    print(f"Batch size: {config.batch_size}")
    print(f"Max concurrent: {config.max_concurrent}")
    print(f"Prompt file: {config.prompt_file}")
    print(f"Input directory: {config.input_dir}")
    print(f"Output directory: {config.output_dir}")
    if config.input_filter:
        print(f"Input filter: {config.input_filter}")
    print("=" * 80)
    
    try:
        # Initialize classifier
        classifier = ArticleClassifier(config)
        classifier.initialize()
        
        # Load articles
        articles = classifier.load_articles()
        
        if not articles:
            print("\nNo unclassified articles found. Exiting.")
            return
        
        # Classify articles
        results = await classifier.classify_articles(articles)
        
        if not results:
            print("\nNo results returned from classification. Exiting.")
            return
        
        # Save results
        output_file = await classifier.save_results(results)
        
        print("\n" + "=" * 80)
        print("Classification complete!")
        print(f"Classified {len(results)} articles")
        print(f"Results saved to: {output_file}")
        print("=" * 80)
        
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
