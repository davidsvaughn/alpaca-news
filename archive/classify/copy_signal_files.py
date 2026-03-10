#!/usr/bin/env python3
"""
Script to copy JSON files from output/alpaca to output/signal based on IDs in signal.tsv
"""

import csv
import glob
import os
import random
import shutil
from pathlib import Path
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def main():
    # Define paths
    signal_tsv_path = Path('classify/signal/signal.tsv')
    source_dir = Path('output/alpaca')
    dest_dir = Path('output/signal')
    
    # Create destination directory if it doesn't exist
    dest_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"Destination directory: {dest_dir}")
    
    # Read IDs from TSV file
    ids = []
    with open(signal_tsv_path, 'r') as f:
        reader = csv.DictReader(f, delimiter='\t')
        for row in reader:
            ids.append(row['id'])
    
    logger.info(f"Found {len(ids)} IDs in {signal_tsv_path}")
    
    # Process each ID
    copied_count = 0
    not_found_count = 0
    multiple_matches_count = 0
    
    for id_value in ids:
        # Find matching files using glob pattern
        pattern = str(source_dir / f"*_{id_value}.json")
        matching_files = glob.glob(pattern)
        
        if not matching_files:
            logger.warning(f"No matching file found for ID: {id_value}")
            not_found_count += 1
            continue
        
        # If multiple files match, log and randomly select one
        if len(matching_files) > 1:
            logger.info(f"Found {len(matching_files)} matching files for ID {id_value}, randomly selecting one")
            multiple_matches_count += 1
            selected_file = random.choice(matching_files)
        else:
            selected_file = matching_files[0]
        
        # Copy the file
        source_file = Path(selected_file)
        dest_file = dest_dir / source_file.name
        
        try:
            shutil.copy2(source_file, dest_file)
            logger.debug(f"Copied: {source_file.name} -> {dest_file}")
            copied_count += 1
        except Exception as e:
            logger.error(f"Error copying {source_file}: {e}")
    
    # Summary
    logger.info("\n" + "="*60)
    logger.info("SUMMARY")
    logger.info("="*60)
    logger.info(f"Total IDs processed: {len(ids)}")
    logger.info(f"Files successfully copied: {copied_count}")
    logger.info(f"IDs with multiple matching files: {multiple_matches_count}")
    logger.info(f"IDs with no matching files: {not_found_count}")
    logger.info("="*60)


if __name__ == "__main__":
    main()
