"""
Kernell OS SDK — Result Merger
══════════════════════════════
Utility class to consolidate results from multiple sub-agents 
into a structured format for the main agent to consume.
"""
import json
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger("kernell.delegation.merger")


class ResultMerger:
    """Consolidates and formats outputs from the worker pool."""
    
    @staticmethod
    def merge_to_json(results: List[str]) -> str:
        """Attempts to parse all worker outputs as JSON and merge them into an array."""
        merged = []
        for res in results:
            try:
                # Try to extract JSON if it's wrapped in markdown blocks
                if "```json" in res:
                    res = res.split("```json")[1].split("```")[0].strip()
                elif "```" in res:
                    res = res.split("```")[1].split("```")[0].strip()
                    
                parsed = json.loads(res)
                if isinstance(parsed, list):
                    merged.extend(parsed)
                else:
                    merged.append(parsed)
            except json.JSONDecodeError:
                # If not valid JSON, append as raw string to avoid losing data
                merged.append({"raw_output": res})
                
        return json.dumps(merged, indent=2)

    @staticmethod
    def merge_to_text(results: List[str], separator: str = "\n---\n") -> str:
        """Concatenates all text outputs with a separator."""
        return separator.join(results)
        
    @staticmethod
    def filter_errors(results: List[str]) -> List[str]:
        """Filters out results that start with standard error prefixes."""
        error_prefixes = ("Error", "Failed", "Exception")
        return [r for r in results if not r.startswith(error_prefixes)]
