"""Enhanced cache management with better organization and performance."""

from __future__ import annotations
import os
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set
from dataclasses import dataclass
import logging

from mind2web2.utils.cache_filesys import CacheFileSys

logger = logging.getLogger(__name__)


@dataclass
class TaskSummary:
    """Task cache summary information."""
    task_id: str
    total_urls: int
    web_urls: int
    pdf_urls: int
    issue_urls: int
    cache_path: str


@dataclass
class URLInfo:
    """URL information with metadata."""
    url: str
    task_id: str
    content_type: str  # "web" or "pdf"
    has_issues: bool = False
    issues: List[str] = None
    
    def __post_init__(self):
        if self.issues is None:
            self.issues = []


class CacheManager:
    """Enhanced cache manager with better performance and organization."""
    
    def __init__(self):
        self.agent_path: Optional[Path] = None
        self.agent_name: str = ""
        self.task_caches: Dict[str, CacheFileSys] = {}
        self.task_summaries: Dict[str, TaskSummary] = {}
        self._url_index: Dict[str, List[URLInfo]] = {}  # url -> [URLInfo]
        
    def load_agent_cache(self, agent_path: str | Path) -> Tuple[int, int]:
        """Load agent cache with improved error handling and progress tracking.
        
        Returns:
            Tuple of (successful_tasks, total_tasks)
        """
        self.agent_path = Path(agent_path)
        self.agent_name = self.agent_path.name
        self.task_caches.clear()
        self.task_summaries.clear()
        self._url_index.clear()
        
        if not self.agent_path.exists():
            raise FileNotFoundError(f"Agent path not found: {agent_path}")
            
        if not self.agent_path.is_dir():
            raise ValueError(f"Path is not a directory: {agent_path}")
        
        task_dirs = [d for d in self.agent_path.iterdir() if d.is_dir()]
        successful_tasks = 0
        
        for task_dir in task_dirs:
            task_id = task_dir.name
            try:
                cache = CacheFileSys(str(task_dir))
                
                # Only load tasks with content
                if self._has_content(cache):
                    self.task_caches[task_id] = cache
                    summary = self._create_task_summary(task_id, cache)
                    self.task_summaries[task_id] = summary
                    self._index_task_urls(task_id, cache)
                    successful_tasks += 1
                    logger.debug(f"Loaded task {task_id} with {summary.total_urls} URLs")
                else:
                    logger.debug(f"Skipped empty task {task_id}")
                    
            except Exception as e:
                logger.warning(f"Failed to load task {task_id}: {e}")
                
        logger.info(f"Loaded {successful_tasks}/{len(task_dirs)} tasks from {self.agent_name}")
        return successful_tasks, len(task_dirs)
    
    def _has_content(self, cache: CacheFileSys) -> bool:
        """Check if cache has any content."""
        try:
            urls = cache.get_all_urls()
            return len(urls) > 0
        except Exception:
            return False
    
    def _create_task_summary(self, task_id: str, cache: CacheFileSys) -> TaskSummary:
        """Create summary information for a task."""
        urls = cache.get_all_urls()
        web_count = 0
        pdf_count = 0
        
        for url in urls:
            content_type = cache.has(url)
            if content_type == "web":
                web_count += 1
            elif content_type == "pdf":
                pdf_count += 1
        
        return TaskSummary(
            task_id=task_id,
            total_urls=len(urls),
            web_urls=web_count,
            pdf_urls=pdf_count,
            issue_urls=0,  # Will be calculated by keyword detector
            cache_path=str(cache.task_dir)
        )
    
    def _index_task_urls(self, task_id: str, cache: CacheFileSys):
        """Index all URLs in a task for efficient lookup."""
        urls = cache.get_all_urls()
        
        for url in urls:
            content_type = cache.has(url)
            url_info = URLInfo(
                url=url,
                task_id=task_id,
                content_type=content_type
            )
            
            if url not in self._url_index:
                self._url_index[url] = []
            self._url_index[url].append(url_info)
    
    def get_task_ids(self) -> List[str]:
        """Get sorted list of task IDs."""
        return sorted(self.task_caches.keys())
    
    def get_task_cache(self, task_id: str) -> Optional[CacheFileSys]:
        """Get cache for specific task."""
        return self.task_caches.get(task_id)
    
    def get_task_summary(self, task_id: str) -> Optional[TaskSummary]:
        """Get summary for specific task."""
        return self.task_summaries.get(task_id)
    
    def get_task_urls(self, task_id: str) -> List[URLInfo]:
        """Get all URLs for a specific task."""
        cache = self.get_task_cache(task_id)
        if not cache:
            return []
        
        urls = cache.get_all_urls()
        url_infos = []
        
        for url in urls:
            content_type = cache.has(url)
            url_info = URLInfo(
                url=url,
                task_id=task_id,
                content_type=content_type
            )
            url_infos.append(url_info)
        
        return url_infos
    
    def find_url_across_tasks(self, url: str) -> List[URLInfo]:
        """Find URL across all tasks."""
        return self._url_index.get(url, [])
    
    def get_url_content(self, task_id: str, url: str, get_screenshot=True) -> Tuple[Optional[str], Optional[bytes]]:
        """Get content for URL (text, screenshot/pdf)."""
        cache = self.get_task_cache(task_id)
        if not cache:
            return None, None
        
        content_type = cache.has(url)
        if content_type == "web":
            try:
                text, screenshot = cache.get_web(url, get_screenshot)
                return text, screenshot
            except Exception as e:
                logger.error(f"Failed to get web content for {url}: {e}")
                return None, None
        elif content_type == "pdf":
            try:
                pdf_bytes = cache.get_pdf(url)
                return None, pdf_bytes
            except Exception as e:
                logger.error(f"Failed to get PDF content for {url}: {e}")
                return None, None
        
        return None, None
    
    def update_url_content(self, task_id: str, url: str, text: str, screenshot: bytes) -> bool:
        """Update web content for URL."""
        cache = self.get_task_cache(task_id)
        if not cache:
            return False
        
        try:
            # Prefer updating the canonical stored URL if it exists
            stored_url = cache._find_url(url)
            target_url = stored_url if stored_url else url
            cache.put_web(target_url, text, screenshot)
            cache.save()
            
            # Update index if it's a new URL
            if target_url not in [info.url for info in self.get_task_urls(task_id)]:
                self._index_single_url(task_id, target_url, "web")
            
            logger.info(f"Updated content for {target_url} in task {task_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to update content for {url}: {e}")
            return False
    
    def add_url_to_task(self, task_id: str, url: str, text: str = None, 
                       screenshot: bytes = None, pdf_bytes: bytes = None) -> bool:
        """Add new URL to task."""
        cache = self.get_task_cache(task_id)
        if not cache:
            return False
        
        try:
            if pdf_bytes:
                cache.put_pdf(url, pdf_bytes)
                content_type = "pdf"
            elif text is not None and screenshot is not None:
                cache.put_web(url, text, screenshot)
                content_type = "web"
            else:
                return False
            
            cache.save()
            self._index_single_url(task_id, url, content_type)
            
            # Update task summary
            summary = self.task_summaries[task_id]
            summary.total_urls += 1
            if content_type == "web":
                summary.web_urls += 1
            else:
                summary.pdf_urls += 1
            
            logger.info(f"Added {url} to task {task_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to add URL {url}: {e}")
            return False
    
    def _index_single_url(self, task_id: str, url: str, content_type: str):
        """Index a single URL."""
        url_info = URLInfo(
            url=url,
            task_id=task_id,
            content_type=content_type
        )
        
        if url not in self._url_index:
            self._url_index[url] = []
        
        # Check if this task already has this URL
        existing = [info for info in self._url_index[url] if info.task_id == task_id]
        if not existing:
            self._url_index[url].append(url_info)
    
    def delete_url(self, task_id: str, url: str) -> bool:
        """Delete URL from task."""
        cache = self.get_task_cache(task_id)
        if not cache:
            return False
        
        try:
            # Get URL hash for file deletion
            url_hash = cache._get_url_hash(url)
            cache_dir = cache.task_dir
            content_type = cache.has(url)
            
            # Delete files
            files_to_delete = []
            if content_type == "web":
                files_to_delete.extend([
                    os.path.join(cache_dir, f"{url_hash}.txt"),
                    os.path.join(cache_dir, f"{url_hash}.jpg")
                ])
            elif content_type == "pdf":
                files_to_delete.append(os.path.join(cache_dir, f"{url_hash}.pdf"))
            
            for file_path in files_to_delete:
                if os.path.exists(file_path):
                    os.remove(file_path)
            
            # Remove from index
            stored_url = cache._find_url(url)
            if stored_url and stored_url in cache.urls:
                del cache.urls[stored_url]
            
            cache.save()
            
            # Update our indexes
            if url in self._url_index:
                self._url_index[url] = [
                    info for info in self._url_index[url] 
                    if info.task_id != task_id
                ]
                if not self._url_index[url]:
                    del self._url_index[url]
            
            # Update summary
            summary = self.task_summaries[task_id]
            summary.total_urls -= 1
            if content_type == "web":
                summary.web_urls -= 1
            else:
                summary.pdf_urls -= 1
            
            logger.info(f"Deleted {url} from task {task_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete URL {url}: {e}")
            return False
    
    def get_all_urls(self) -> List[str]:
        """Get all unique URLs across all tasks."""
        return list(self._url_index.keys())
    
    # --- Reviewed status persistence ---

    def _reviewed_path(self, task_id: str) -> Path:
        """Return path to the reviewed.json file for a task."""
        cache = self.task_caches.get(task_id)
        if cache:
            return Path(cache.task_dir) / "reviewed.json"
        return Path()

    def load_reviewed(self, task_id: str) -> Dict[str, str]:
        """Load reviewed statuses for a task.

        Returns:
            Dict mapping url -> status ("ok", "fixed", "skip").
        """
        path = self._reviewed_path(task_id)
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
            except Exception as e:
                logger.warning(f"Failed to load reviewed.json for {task_id}: {e}")
        return {}

    def save_reviewed(self, task_id: str, reviewed_map: Dict[str, str]):
        """Save reviewed statuses for a task."""
        path = self._reviewed_path(task_id)
        if not path.parent.exists():
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(reviewed_map, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save reviewed.json for {task_id}: {e}")

    def mark_url_reviewed(self, task_id: str, url: str, status: str):
        """Mark a single URL as reviewed and persist."""
        reviewed = self.load_reviewed(task_id)
        if status:
            reviewed[url] = status
        else:
            reviewed.pop(url, None)
        self.save_reviewed(task_id, reviewed)

    def get_statistics(self) -> Dict[str, int]:
        """Get overall statistics."""
        total_urls = len(self._url_index)
        total_tasks = len(self.task_caches)
        total_web = sum(1 for infos in self._url_index.values()
                       for info in infos if info.content_type == "web")
        total_pdf = sum(1 for infos in self._url_index.values()
                       for info in infos if info.content_type == "pdf")

        return {
            "total_tasks": total_tasks,
            "total_urls": total_urls,
            "web_urls": total_web,
            "pdf_urls": total_pdf
        }
