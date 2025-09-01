import os
import time
import json
import uuid
import shutil
import threading
import requests
import urllib3
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, render_template_string, jsonify, send_from_directory
from bs4 import BeautifulSoup
from PIL import Image, ImageFilter, ImageEnhance, ImageDraw
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Optional dependencies
try:
    from backgroundremover.bg import remove as remove_bg
    REMBG_AVAILABLE = True
except ImportError:
    REMBG_AVAILABLE = False
    print("BackgroundRemover not available. Install with: pip install backgroundremover")

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False
    print("OpenAI not available. Install with: pip install openai")

try:
    from transformers import CLIPProcessor, CLIPModel
    import torch
    CLIP_AVAILABLE = True
except ImportError:
    CLIP_AVAILABLE = False
    print("Transformers/CLIP not available. Install with: pip install transformers torch")

# Configure requests with retry strategy
session = requests.Session()
retries = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
session.mount('https://', HTTPAdapter(max_retries=retries))

from flask import Flask
app = Flask(__name__)

# Configuration
BASE_DIR = Path(__file__).parent.absolute()
DOWNLOADS_DIR = BASE_DIR / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)

# Global job storage
JOBS = {}
STOP_FLAGS = {}
JOBS_LOCK = threading.Lock()

# ----------------------- Core Scraping Functions -----------------------

def log_message(job_id, message, log_type="info"):
    """Add real-time log message"""
    with JOBS_LOCK:
        if job_id in JOBS:
            timestamp = time.strftime('%H:%M:%S')
            emoji = {"info": "ℹ️", "success": "✅", "error": "❌", "warning": "⚠️"}.get(log_type, "")
            log_entry = f"[{timestamp}] {emoji} {message}"
            JOBS[job_id]['logs'].insert(0, log_entry)
            JOBS[job_id]['logs'] = JOBS[job_id]['logs'][:100]  # Keep last 100
            print(log_entry)  # Also print to console

def fetch_product_urls(job_id, store_url, max_products=None):
    """Fetch eBay listing URLs from store, single item, or search results page"""
    try:
        product_urls = []
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5"
        }
        if '/itm/' in store_url:
            clean_url = store_url.split('?')[0]
            product_urls = [clean_url]
            log_message(job_id, f"Processing single item URL: {clean_url}", "info")
        elif '/sch/i.html' in store_url:
            page = 1
            while True:
                if STOP_FLAGS.get(job_id, False):
                    log_message(job_id, "Stopped during product discovery", "warning")
                    break
                page_url = f"{store_url}&_pgn={page}" if page > 1 else store_url
                resp = session.get(page_url, verify=False, timeout=15, headers=headers)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.content, "html.parser")
                item_links = soup.find_all('a', class_='s-item__link')
                if not item_links:
                    log_message(job_id, f"No item links found on search page {page}", "warning")
                    break
                for link in item_links:
                    href = link.get('href')
                    if href and '/itm/' in href:
                        clean_href = href.split('?')[0]
                        product_urls.append(clean_href)
                        if max_products and len(product_urls) >= max_products:
                            return product_urls, None
                next_button = soup.find('a', class_='pagination__next')
                if not next_button or 'disabled' in next_button.get('class', []):
                    break
                page += 1
                time.sleep(2)
        else:
            page = 1
            while True:
                if STOP_FLAGS.get(job_id, False):
                    log_message(job_id, "Stopped during product discovery", "warning")
                    break
                page_url = f"{store_url}?_pgn={page}" if page > 1 else store_url
                resp = session.get(page_url, verify=False, timeout=15, headers=headers)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.content, "html.parser")
                item_links = soup.find_all('a', class_='s-item__link')
                if not item_links:
                    log_message(job_id, f"No item links found on store page {page}", "warning")
                    break
                for link in item_links:
                    href = link.get('href')
                    if href and '/itm/' in href:
                        clean_href = href.split('?')[0]
                        product_urls.append(clean_href)
                        if max_products and len(product_urls) >= max_products:
                            return product_urls, None
                next_button = soup.find('a', class_='pagination__next')
                if not next_button or 'disabled' in next_button.get('class', []):
                    break
                page += 1
                time.sleep(2)
        if not product_urls:
            log_message(job_id, "No product URLs found", "error")
            return [], "No product URLs found"
        return product_urls, None
    except Exception as e:
        log_message(job_id, f"Product discovery failed: {str(e)}", "error")
        return [], str(e)

def scrape_product_images(job_id, url):
    """Scrape product images from eBay item page with robust selectors"""
    try:
        log_message(job_id, f"Scraping images from: {url}", "info")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5"
        }
        page = session.get(url, verify=False, timeout=20, headers=headers)
        page.raise_for_status()
        soup = BeautifulSoup(page.content, "html.parser")
        img_urls = set()

        # 1. JSON-LD structured data
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.get_text(strip=True))
                if isinstance(data, dict) and "image" in data:
                    images = data["image"]
                    if isinstance(images, str):
                        images = [images]
                    img_urls.update([img for img in images if "ebayimg.com" in img])
            except json.JSONDecodeError:
                log_message(job_id, "Failed to parse JSON-LD data", "warning")
                continue

        # 2. Inline state JSON (window.__INIT_STATE__)
        for script in soup.find_all("script"):
            script_text = script.get_text(strip=True)
            if "window.__INIT_STATE__" in script_text:
                try:
                    start = script_text.find("{")
                    end = script_text.rfind("}") + 1
                    json_str = script_text[start:end]
                    data = json.loads(json_str)
                    media = data.get("media", {}).get("gallery", {}).get("mediaList", []) or []
                    for item in media:
                        if "image" in item:
                            img_url = item["image"].get("href") or item["image"].get("URL")
                            if img_url and "ebayimg.com" in img_url:
                                img_urls.add(img_url.replace("s-l64.jpg", "s-l1600.jpg"))
                except json.JSONDecodeError:
                    log_message(job_id, "Failed to parse inline JSON state", "warning")
                    continue

        # 3. Fallback HTML selectors
        selectors = [
            "img#icImg",
            "img[src*='ebayimg.com']",
            "img[data-src*='ebayimg.com']",
            ".ux-image-carousel-item img",
            ".ux-image-magnifier img",
            ".image-treatment img",
            ".ux-image-gallery img",
            "div[itemprop='image'] img"
        ]
        for selector in selectors:
            for img in soup.select(selector):
                src = img.get("src") or img.get("data-src") or img.get("data-zoom-img")
                if src and "ebayimg.com" in src:
                    src = src.replace("s-l64.jpg", "s-l1600.jpg").replace("s-l225.jpg", "s-l1600.jpg")
                    img_urls.add(src)

        # Filter valid image URLs
        img_urls = [u for u in img_urls if any(u.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp")) and "ebayimg.com" in u]
        
        if not img_urls:
            debug_path = DOWNLOADS_DIR / f"{job_id}_debug.html"
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write(page.text)
            log_message(job_id, f"No images found for {url.split('/')[-1]}. Debug HTML saved to {debug_path}", "warning")
        else:
            log_message(job_id, f"Found {len(img_urls)} images for {url.split('/')[-1]}", "success")
        
        return list(img_urls)
    except Exception as e:
        log_message(job_id, f"Error scraping images from {url}: {str(e)}", "error")
        return []

def download_single_image(args):
    """Download single image with timing"""
    img_url, save_dir, job_id = args
    
    if STOP_FLAGS.get(job_id, False):
        return {"url": img_url, "status": "stopped", "filename": None, "elapsed": 0}
    
    try:
        filename = img_url.split("/")[-1].split("?")[0] or f"image_{int(time.time())}.jpg"
        filepath = save_dir / filename
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5"
        }
        start = time.time()
        response = session.get(img_url, verify=False, timeout=15, headers=headers, stream=True)
        response.raise_for_status()
        
        with open(filepath, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        
        # Create thumbnail for faster preview
        try:
            with Image.open(filepath) as img:
                img.thumbnail((300, 300), Image.LANCZOS)
                img.save(filepath, quality=85, optimize=True)
        except:
            pass
        
        elapsed = time.time() - start
        log_message(job_id, f"Downloaded {filename} in {elapsed:.2f}s" + (" (slow)" if elapsed > 2 else ""), 
                   "success" if elapsed <= 2 else "warning")
        
        return {"url": img_url, "status": "success", "filename": filename, "elapsed": elapsed}
        
    except Exception as e:
        log_message(job_id, f"Failed to download {img_url}: {str(e)}", "error")
        return {"url": img_url, "status": "error", "filename": None, "elapsed": 0, "error": str(e)}

def run_scraping_job(job_id):
    """Main scraping workflow with concurrent execution"""
    with JOBS_LOCK:
        meta = JOBS.get(job_id)
        if not meta:
            log_message(job_id, "Job not found", "error")
            print(f"Job {job_id} not found in JOBS dictionary")
            return
    try:
        log_message(job_id, "Starting product discovery...", "info")
        meta['state'] = 'discovering'
        store_url = meta['store_url']
        max_products = meta['options'].get('max_products', 30)
        product_urls, error = fetch_product_urls(job_id, store_url, max_products)
        if error:
            raise Exception(f"Product discovery failed: {error}")
        if not product_urls:
            raise Exception("No products found in store, item, or search results page")
        log_message(job_id, f"Found {len(product_urls)} product pages", "success")
        meta['stats']['products'] = len(product_urls)
        if STOP_FLAGS.get(job_id, False):
            meta['state'] = 'stopped'
            log_message(job_id, "Job stopped during discovery", "warning")
            return
        log_message(job_id, "Discovering product images...", "info")
        meta['state'] = 'scraping_images'
        all_images = []
        max_workers = min(meta['options'].get('max_workers', 8), 12)
        def scrape_single_product(url):
            if STOP_FLAGS.get(job_id, False):
                return []
            images = scrape_product_images(job_id, url)
            return images
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_url = {executor.submit(scrape_single_product, url): url for url in product_urls}
            for future in as_completed(future_to_url):
                if STOP_FLAGS.get(job_id, False):
                    break
                try:
                    images = future.result()
                    all_images.extend(images)
                except Exception as e:
                    url = future_to_url[future]
                    log_message(job_id, f"Error scraping {url}: {e}", "error")
                    meta['stats']['errors'] += 1
        unique_images = list(set(all_images))
        log_message(job_id, f"Found {len(unique_images)} unique images (removed {len(all_images) - len(unique_images)} duplicates)", "success")
        meta['stats']['images'] = len(unique_images)
        if not unique_images:
            raise Exception("No images found across all products")
        if STOP_FLAGS.get(job_id, False):
            meta['state'] = 'stopped'
            log_message(job_id, "Job stopped during image scraping", "warning")
            return
        log_message(job_id, "Starting parallel downloads...", "info")
        meta['state'] = 'downloading'
        save_dir = Path(meta['folder'])
        download_args = [(img_url, save_dir, job_id) for img_url in unique_images]
        results = []
        download_workers = min(meta['options'].get('max_workers', 8), 15)
        with ThreadPoolExecutor(max_workers=download_workers) as executor:
            future_to_img = {executor.submit(download_single_image, args): args[0] for args in download_args}
            for future in as_completed(future_to_img):
                if STOP_FLAGS.get(job_id, False):
                    break
                try:
                    result = future.result()
                    results.append(result)
                    if result['status'] == 'success':
                        meta['stats']['downloaded'] += 1
                    else:
                        meta['stats']['errors'] += 1
                    progress = (len(results) / len(unique_images)) * 100
                    log_message(job_id, f"Progress: {len(results)}/{len(unique_images)} ({progress:.1f}%)", "info")
                except Exception as e:
                    img_url = future_to_img[future]
                    log_message(job_id, f"Download error for {img_url}: {e}", "error")
                    meta['stats']['errors'] += 1
        successful_downloads = [r for r in results if r['status'] == 'success']
        if successful_downloads:
            avg_time = sum(r['elapsed'] for r in successful_downloads) / len(successful_downloads)
            meta['avg_time'] = avg_time
            log_message(job_id, f"Average download time: {avg_time:.2f}s", "success")
        meta['images'] = [
            {
                'url': r['url'],
                'filename': r.get('filename'),
                'elapsed': r.get('elapsed'),
                'error': r.get('error'),
                'processed_filename': None,
                'bg_removed_filename': None,
                'bg_generated_filename': None
            }
            for r in results
        ]
        if STOP_FLAGS.get(job_id, False):
            meta['state'] = 'stopped'
            log_message(job_id, "Job stopped during downloading", "warning")
            return
        log_message(job_id, "Creating ZIP archive...", "info")
        zip_path = DOWNLOADS_DIR / f"{job_id}.zip"
        shutil.make_archive(str(zip_path.with_suffix('')), 'zip', str(save_dir))
        log_message(job_id, f"ZIP created: {zip_path.name}", "success")
        meta['state'] = 'completed'
        log_message(job_id, f"Scraping completed! Downloaded {meta['stats']['downloaded']} images", "success")
    except Exception as e:
        log_message(job_id, f"Scraping failed: {str(e)}", "error")
        meta['state'] = 'failed'

def generate_background(width, height, bg_type):
    """Generate a background image (white or lifestyle/studio)."""
    try:
        img = Image.new('RGBA', (width, height), (255, 255, 255, 255))  # Default white background
        draw = ImageDraw.Draw(img)
        
        if bg_type == 'lifestyle':
            # Simulate lifestyle background with a soft gradient
            colors = [(200, 220, 255), (230, 240, 255)]  # Light blue gradient
            for y in range(height):
                r = int(colors[0][0] + (colors[1][0] - colors[0][0]) * y / height)
                g = int(colors[0][1] + (colors[1][1] - colors[0][1]) * y / height)
                b = int(colors[0][2] + (colors[1][2] - colors[0][2]) * y / height)
                draw.line((0, y, width, y), fill=(r, g, b, 255))
        elif bg_type == 'studio':
            # Simulate studio background with a subtle radial gradient
            center = (width // 2, height // 2)
            max_radius = (width**2 + height**2)**0.5 / 2
            for y in range(height):
                for x in range(width):
                    radius = ((x - center[0])**2 + (y - center[1])**2)**0.5
                    intensity = min(255, int(255 * (1 - radius / max_radius)))
                    draw.point((x, y), fill=(intensity, intensity, intensity, 255))
        
        return img, None
    except Exception as e:
        return None, f"Failed to generate background: {str(e)}"

def apply_background(foreground_path, output_path, bg_type):
    """Apply generated background to a foreground image."""
    try:
        with Image.open(foreground_path) as fg_img:
            fg_img = fg_img.convert('RGBA')
            width, height = fg_img.size
            
            bg_img, error = generate_background(width, height, bg_type)
            if error:
                return False, error
            
            # Composite foreground onto background
            result = Image.alpha_composite(bg_img, fg_img)
            result.save(output_path, 'PNG', optimize=True)
            return True, None
    except Exception as e:
        return False, f"Failed to apply background: {str(e)}"

def process_background_generation_job(job_id, filenames, bg_type):
    """Process selected images for background generation."""
    try:
        meta = JOBS[job_id]
        meta['state'] = 'bg_generating'
        
        log_message(job_id, f"Starting background generation ({bg_type}) for {len(filenames)} images...", "info")
        
        save_dir = Path(meta['folder'])
        bg_generated_dir = save_dir / 'bg_generated'
        bg_generated_dir.mkdir(exist_ok=True)
        
        processed_count = 0
        for filename in filenames:
            if STOP_FLAGS.get(job_id, False):
                log_message(job_id, "Background generation stopped", "warning")
                meta['state'] = 'stopped'
                return
            
            # Use background-removed image if available, else original
            input_path = None
            for img in meta['images']:
                if img.get('filename') == filename:
                    if img.get('bg_removed_filename'):
                        input_path = save_dir / 'bg_removed' / img['bg_removed_filename']
                    else:
                        input_path = save_dir / filename
                    break
            
            if not input_path or not input_path.exists():
                log_message(job_id, f"File not found: {filename}", "error")
                meta['stats']['errors'] += 1
                continue
            
            output_filename = f"{input_path.stem}_{bg_type}.png"
            output_path = bg_generated_dir / output_filename
            
            start_time = time.time()
            success, error = apply_background(input_path, output_path, bg_type)
            
            if success:
                elapsed = time.time() - start_time
                log_message(job_id, f"Background generated: {filename} -> {output_filename} ({elapsed:.2f}s)", "success")
                processed_count += 1
                
                # Update metadata
                with JOBS_LOCK:
                    for img in meta['images']:
                        if img.get('filename') == filename:
                            img['bg_generated_filename'] = output_filename
                            break
            else:
                log_message(job_id, error, "error")
                meta['stats']['errors'] += 1
        
        # Create ZIP for background-generated images
        try:
            zip_path = DOWNLOADS_DIR / f"{job_id}_bg_generated.zip"
            shutil.make_archive(str(zip_path.with_suffix('')), 'zip', str(bg_generated_dir))
            log_message(job_id, f"Created background-generated ZIP: {processed_count} images", "success")
        except Exception as e:
            log_message(job_id, f"Background-generated ZIP creation failed: {str(e)}", "error")
            meta['stats']['errors'] += 1
        
        if processed_count > 0:
            meta['state'] = 'bg_generated'
            log_message(job_id, f"Background generation ({bg_type}) completed successfully!", "success")
        else:
            meta['state'] = 'failed'
            log_message(job_id, "Background generation failed: No images processed", "error")
    
    except Exception as e:
        log_message(job_id, f"Background generation job failed: {str(e)}", "error")
        meta['state'] = 'failed'

try:
    import cv2
    import numpy as np
    OPENCV_AVAILABLE = True
except ImportError:
    OPENCV_AVAILABLE = False
    print("OpenCV not available. Install with: pip install opencv-python")

def remove_background_pillow(input_path, output_path):
    """Remove near-white background from an image using Pillow."""
    try:
        with Image.open(input_path) as img:
            img = img.convert('RGBA')  # Ensure image has alpha channel
            pixels = img.load()
            width, height = img.size

            # Threshold for near-white pixels (adjustable)
            threshold = 200

            for x in range(width):
                for y in range(height):
                    r, g, b, a = pixels[x, y]
                    # If pixel is near-white, set alpha to 0 (transparent)
                    if r > threshold and g > threshold and b > threshold:
                        pixels[x, y] = (r, g, b, 0)

            img.save(output_path, 'PNG', optimize=True)
            return True, None
    except Exception as e:
        return False, f"Failed to process {input_path.name}: {str(e)}"

def remove_background(input_path, output_path):
    """Remove background using OpenCV or fallback to Pillow."""
    if OPENCV_AVAILABLE:
        try:
            # Read image with OpenCV
            img = cv2.imread(str(input_path))
            if img is None:
                return False, f"Failed to load image {input_path.name}"

            # Convert to RGB (OpenCV uses BGR by default)
            img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            height, width = img_rgb.shape[:2]

            # Create a mask for GrabCut
            mask = np.zeros((height, width), np.uint8)
            bgd_model = np.zeros((1, 65), np.float64)
            fgd_model = np.zeros((1, 65), np.float64)

            # Define a rectangle for GrabCut (adjustable, here we use the entire image with a margin)
            rect = (10, 10, width - 20, height - 20)

            # Apply GrabCut
            cv2.grabCut(img_rgb, mask, rect, bgd_model, fgd_model, 5, cv2.GC_INIT_WITH_RECT)

            # Create mask where 0 and 2 are background, 1 and 3 are foreground
            mask2 = np.where((mask == 2) | (mask == 0), 0, 1).astype('uint8')

            # Apply mask to create transparent background
            img_rgba = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2RGBA)
            img_rgba[:, :, 3] = mask2 * 255  # Set alpha channel

            # Save the output
            cv2.imwrite(str(output_path), img_rgba)
            return True, None
        except Exception as e:
            return False, f"Failed to process {input_path.name}: {str(e)}"
    else:
        return remove_background_pillow(input_path, output_path)

def process_background_removal_job(job_id, filenames):
    """Process selected images for background removal."""
    try:
        meta = JOBS[job_id]
        meta['state'] = 'bg_removing'
        
        log_message(job_id, f"Starting background removal for {len(filenames)} images...", "info")
        
        save_dir = Path(meta['folder'])
        bg_removed_dir = save_dir / 'bg_removed'
        bg_removed_dir.mkdir(exist_ok=True)
        
        processed_count = 0
        for filename in filenames:
            if STOP_FLAGS.get(job_id, False):
                log_message(job_id, "Background removal stopped", "warning")
                meta['state'] = 'stopped'
                return
            
            input_path = save_dir / filename
            if not input_path.exists():
                log_message(job_id, f"File not found: {filename}", "error")
                meta['stats']['errors'] += 1
                continue
            
            output_filename = f"{input_path.stem}_nobg.png"
            output_path = bg_removed_dir / output_filename
            
            start_time = time.time()
            success, error = remove_background(input_path, output_path)
            
            if success:
                elapsed = time.time() - start_time
                log_message(job_id, f"Background removed: {filename} -> {output_filename} ({elapsed:.2f}s)", "success")
                processed_count += 1
                
                # Update metadata
                with JOBS_LOCK:
                    for img in meta['images']:
                        if img.get('filename') == filename:
                            img['bg_removed_filename'] = output_filename
                            break
            else:
                log_message(job_id, error, "error")
                meta['stats']['errors'] += 1
        
        # Create ZIP for background-removed images
        try:
            zip_path = DOWNLOADS_DIR / f"{job_id}_bg_removed.zip"
            shutil.make_archive(str(zip_path.with_suffix('')), 'zip', str(bg_removed_dir))
            log_message(job_id, f"Created background-removed ZIP: {processed_count} images", "success")
        except Exception as e:
            log_message(job_id, f"Background-removed ZIP creation failed: {str(e)}", "error")
            meta['stats']['errors'] += 1
        
        if processed_count > 0:
            meta['state'] = 'bg_removed'
            log_message(job_id, "Background removal completed successfully!", "success")
        else:
            meta['state'] = 'failed'
            log_message(job_id, "Background removal failed: No images processed", "error")
    
    except Exception as e:
        log_message(job_id, f"Background removal job failed: {str(e)}", "error")
        meta['state'] = 'failed'

def process_images_job(job_id, filenames, options):
    """Process images with real-time logging"""
    meta = JOBS[job_id]
    meta['state'] = 'processing'
    
    log_message(job_id, f"Starting processing of {len(filenames)} images...", "info")
    
    save_dir = Path(meta['folder'])
    processed_dir = save_dir / 'processed'
    processed_dir.mkdir(exist_ok=True)
    
    def process_single_image(filename):
        if STOP_FLAGS.get(job_id, False):
            return None
        
        input_path = save_dir / filename
        if not input_path.exists():
            log_message(job_id, f"File not found: {filename}", "error")
            return None
        
        base_name = input_path.stem
        start_time = time.time()
        
        try:
            current_path = input_path
            
            # Remove background if requested
            if options.get('remove_bg') and REMBG_AVAILABLE:
                log_message(job_id, f"Removing background: {filename}", "info")
                bg_removed_path = processed_dir / f"{base_name}_nobg.png"
                success, error = remove_background(input_path, bg_removed_path)
                if success:
                    current_path = bg_removed_path
                    log_message(job_id, f"Background removed: {filename}", "success")
                else:
                    log_message(job_id, error, "error")
                    return None
            
            # Apply image enhancements
            with Image.open(current_path) as img:
                img = img.convert('RGB')
                
                # Brightness/Contrast
                if options.get('brightness', 1.0) != 1.0:
                    img = ImageEnhance.Brightness(img).enhance(float(options['brightness']))
                
                if options.get('contrast', 1.0) != 1.0:
                    img = ImageEnhance.Contrast(img).enhance(float(options['contrast']))
                
                # Upscaling
                if options.get('upscale'):
                    log_message(job_id, f"Upscaling: {filename}", "info")
                    w, h = img.size
                    img = img.resize((w*2, h*2), Image.LANCZOS)
                
                # Sharpening
                if options.get('enhance'):
                    img = img.filter(ImageFilter.SHARPEN)
                
                # Platform sizing
                platform = options.get('platform', 'none')
                if platform != 'none':
                    min_sizes = {'amazon': 1000, 'ebay': 500, 'shopify': 2048}
                    min_size = min_sizes.get(platform, 1000)
                    w, h = img.size
                    if min(w, h) < min_size:
                        scale = min_size / min(w, h)
                        img = img.resize((int(w*scale), int(h*scale)), Image.LANCZOS)
                
                # Save with format conversion
                fmt = options.get('format', 'PNG').upper()
                ext = 'jpg' if fmt in ('JPG', 'JPEG') else fmt.lower()
                output_path = processed_dir / f"{base_name}_processed.{ext}"
                
                if fmt in ('JPG', 'JPEG'):
                    img.save(output_path, 'JPEG', quality=int(options.get('quality', 85)), optimize=True)
                elif fmt == 'WEBP':
                    img.save(output_path, 'WEBP', quality=int(options.get('quality', 85)), optimize=True)
                else:
                    img.save(output_path, 'PNG', optimize=True)
            
            elapsed = time.time() - start_time
            log_message(job_id, f"Processed {filename} -> {output_path.name} ({elapsed:.2f}s)", "success")
            
            # Update metadata
            with JOBS_LOCK:
                for img in meta['images']:
                    if img.get('filename') == filename:
                        img['processed_filename'] = output_path.name
                        break
            
            return output_path.name
            
        except Exception as e:
            log_message(job_id, f"Processing error for {filename}: {e}", "error")
            return None
    
    # Process in parallel
    max_workers = min(options.get('workers', 4), 8)
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(process_single_image, fname) for fname in filenames]
        
        processed_count = 0
        for future in as_completed(futures):
            if STOP_FLAGS.get(job_id, False):
                break
            try:
                result = future.result()
                if result:
                    processed_count += 1
            except Exception as e:
                log_message(job_id, f"Processing thread error: {e}", "error")
    
    # Create processed ZIP
    try:
        zip_path = DOWNLOADS_DIR / f"{job_id}_processed.zip"
        shutil.make_archive(str(zip_path.with_suffix('')), 'zip', str(processed_dir))
        log_message(job_id, f"Created processed ZIP: {processed_count} images", "success")
    except Exception as e:
        log_message(job_id, f"ZIP creation failed: {e}", "error")
    
    meta['state'] = 'processed'
    log_message(job_id, "Processing completed!", "success")

# ----------------------- Flask Routes -----------------------

@app.route('/')
def index():
    return render_template_string("""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>eBay Image Scraper</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        body { font-family: 'Inter', sans-serif; }
        .log-entry { animation: fadeIn 0.3s ease-in; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(-10px); } to { opacity: 1; transform: translateY(0); } }
    </style>
</head>
<body class="bg-gray-50">
    <div class="min-h-screen p-6">
        <div class="max-w-6xl mx-auto">
            <!-- Header -->
            <div class="text-center mb-8">
                <h1 class="text-4xl font-bold text-gray-900 mb-2">🕷️ eBay Image Scraper</h1>
                <p class="text-gray-600">Fast concurrent scraping with real-time logs</p>
                <div class="text-sm text-gray-500 mt-2">
                    BackgroundRemover: {{ '✅ Available' if rembg_available else '❌ Not installed' }}
                </div>
            </div>

            <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
                <!-- Scraping Panel -->
                <div class="bg-white rounded-lg shadow-lg p-6">
                    <h2 class="text-xl font-semibold mb-4">🚀 Start Scraping</h2>
                    
                    <div class="space-y-4">
                        <div>
                            <label class="block text-sm font-medium mb-1">eBay Store, Item, or Search URL</label>
                            <input id="storeUrl" type="url" placeholder="e.g., https://www.ebay.com/str/sellerusername, https://www.ebay.com/itm/123456789012, or https://www.ebay.com/sch/i.html?..."
                                   class="w-full px-3 py-2 border border-gray-300 rounded-md focus:ring-2 focus:ring-blue-500">
                            <div id="imageCount" class="text-sm text-gray-500 mt-1"></div>
                        </div>
                        
                        <div class="grid grid-cols-2 gap-4">
                            <div>
                                <label class="block text-sm font-medium mb-1">Max Products</label>
                                <input id="maxProducts" type="number" value="30" min="1" max="500"
                                       class="w-full px-3 py-2 border border-gray-300 rounded-md">
                            </div>
                            <div>
                                <label class="block text-sm font-medium mb-1">Workers</label>
                                <input id="maxWorkers" type="number" value="8" min="1" max="15"
                                       class="w-full px-3 py-2 border border-gray-300 rounded-md">
                            </div>
                        </div>
                        
                        <div class="flex items-center">
                            <input id="ignoreSSL" type="checkbox" class="mr-2" checked>
                            <label class="text-sm">Ignore SSL errors (recommended)</label>
                        </div>
                        
                        <div class="flex gap-3">
                            <button id="startBtn" onclick="startScrape()" 
                                    class="bg-green-600 text-white px-6 py-2 rounded-md font-medium hover:bg-green-700 transition">
                                🚀 Start Scrape
                            </button>
                            <button id="stopBtn" onclick="stopScrape()" 
                                    class="bg-red-600 text-white px-6 py-2 rounded-md font-medium hover:bg-red-700 transition hidden">
                                🛑 Stop
                            </button>
                        </div>
                    </div>

                    <!-- Progress -->
                    <div id="progressPanel" class="mt-6 hidden">
                        <div class="flex justify-between items-center mb-2">
                            <span id="statusText" class="font-medium">Starting...</span>
                            <span id="progressPercent" class="text-sm text-gray-600">0%</span>
                        </div>
                        <div class="w-full bg-gray-200 rounded-full h-3 overflow-hidden">
                            <div id="progressBar" class="bg-gradient-to-r from-green-500 to-blue-500 h-3 rounded-full transition-all duration-300" style="width: 0%"></div>
                        </div>
                        <div id="statsText" class="text-sm text-gray-600 mt-2"></div>
                    </div>
                </div>

                <!-- Real-time Logs -->
                <div class="bg-white rounded-lg shadow-lg p-6">
                    <h2 class="text-xl font-semibold mb-4">📋 Real-time Logs</h2>
                    <div id="logsContainer" class="bg-gray-900 text-green-400 p-4 rounded-md h-96 overflow-y-auto font-mono text-sm">
                        <div class="text-gray-500">Logs will appear here...</div>
                    </div>
                    <div class="mt-3 flex justify-between text-xs text-gray-500">
                        <span>Auto-scroll: <input id="autoScroll" type="checkbox" checked></span>
                        <button onclick="clearLogs()" class="text-red-600 hover:text-red-800">Clear Logs</button>
                    </div>
                </div>
            </div>

            <!-- Results Panel -->
            <div id="resultsPanel" class="mt-8 hidden">
                <div class="bg-white rounded-lg shadow-lg p-6">
                    <div class="flex justify-between items-center mb-4">
                        <h2 class="text-xl font-semibold">📸 Downloaded Images</h2>
                        <div class="flex gap-3">
                            <a id="downloadLink" href="#" class="bg-blue-600 text-white px-4 py-2 rounded-md hover:bg-blue-700">
                                📥 Download ZIP
                            </a>
                            <a id="viewLink" href="#" class="bg-purple-600 text-white px-4 py-2 rounded-md hover:bg-purple-700">
                                👁️ View Gallery
                            </a>
                        </div>
                    </div>
                    <div id="imagePreview" class="grid grid-cols-4 md:grid-cols-8 gap-2"></div>
                </div>
            </div>
        </div>
    </div>

    <script>
    let currentJobId = null;
    let polling = false;

    async function startScrape() {
        const url = document.getElementById('storeUrl').value.trim();
        if (!url) {
            alert('Please enter a store, item, or search URL');
            return;
        }

        const formData = new FormData();
        formData.append('store_url', url);
        formData.append('max_products', document.getElementById('maxProducts').value);
        formData.append('max_workers', document.getElementById('maxWorkers').value);
        if (document.getElementById('ignoreSSL').checked) {
            formData.append('ignore_ssl', 'true');
        }

        try {
            // Update UI
            document.getElementById('startBtn').disabled = true;
            document.getElementById('startBtn').innerHTML = '⏳ Starting...';
            document.getElementById('stopBtn').classList.remove('hidden');
            document.getElementById('progressPanel').classList.remove('hidden');
            clearLogs();

            const response = await fetch('/api/start', {
                method: 'POST',
                body: formData
            });

            const result = await response.json();
            
            if (result.error) {
                alert(result.error);
                resetUI();
                return;
            }

            currentJobId = result.job_id;
            addLog(`🎯 Started job: ${currentJobId}`, 'info');
            
            // Update download links
            document.getElementById('downloadLink').href = `/download/${currentJobId}.zip`;
            document.getElementById('viewLink').href = `/view/${currentJobId}`;
            
            startPolling();
            
        } catch (error) {
            alert('Failed to start scrape: ' + error.message);
            resetUI();
        }
    }

    async function stopScrape() {
        if (!currentJobId) return;
        
        try {
            await fetch(`/api/stop/${currentJobId}`, { method: 'POST' });
            addLog('🛑 Stop signal sent', 'warning');
            polling = false;
            setTimeout(resetUI, 1000);
        } catch (error) {
            alert('Failed to stop scrape: ' + error.message);
        }
    }

    function resetUI() {
        document.getElementById('startBtn').disabled = false;
        document.getElementById('startBtn').innerHTML = '🚀 Start Scrape';
        document.getElementById('stopBtn').classList.add('hidden');
        document.getElementById('progressPanel').classList.add('hidden');
    }

    async function startPolling() {
        polling = true;
        let lastLogCount = 0;
        
        while (polling && currentJobId) {
            try {
                const response = await fetch(`/api/status/${currentJobId}`);
                const status = await response.json();
                
                if (status.error) {
                    addLog(`❌ Error: ${status.error}`, 'error');
                    polling = false;
                    resetUI();
                    break;
                }

                // Update status
                const stateEmojis = {
                    'discovering': '🔍',
                    'scraping_images': '🕷️',
                    'downloading': '📥',
                    'processing': '⚙️',
                    'completed': '✅',
                    'failed': '❌',
                    'stopped': '🛑',
                    'bg_removing': '🧹',
                    'bg_removed': '✅',
                    'bg_generating': '🎨',
                    'bg_generated': '✅'
                };
                
                const stateText = stateEmojis[status.state] + ' ' + status.state.replace('_', ' ').toUpperCase();
                document.getElementById('statusText').textContent = stateText;
                
                const stats = status.stats;
                const progress = stats.images > 0 ? Math.round((stats.downloaded / stats.images) * 100) : 0;
                document.getElementById('progressPercent').textContent = progress + '%';
                document.getElementById('progressBar').style.width = progress + '%';
                
                document.getElementById('statsText').textContent = 
                    `Products: ${stats.products} | Images: ${stats.images} | Downloaded: ${stats.downloaded} | Errors: ${stats.errors}`;

                // Update logs (only new ones)
                if (status.logs.length > lastLogCount) {
                    const newLogs = status.logs.slice(0, status.logs.length - lastLogCount);
                    newLogs.reverse().forEach(log => addLog(log, 'stream'));
                    lastLogCount = status.logs.length;
                }
                
                // Handle completion
                if (['completed', 'failed', 'stopped', 'processed', 'bg_removed', 'bg_generated'].includes(status.state)) {
                    polling = false;
                    resetUI();
                    
                    if (status.state === 'completed') {
                        showResults();
                    }
                }
                
            } catch (error) {
                console.error('Polling error:', error);
                addLog('⚠️ Connection error during polling', 'warning');
            }
            
            await new Promise(resolve => setTimeout(resolve, 500)); // Fast polling for real-time feel
        }
    }

    function addLog(message, type = 'info') {
        const container = document.getElementById('logsContainer');
        const logDiv = document.createElement('div');
        logDiv.className = 'log-entry mb-1';
        
        const colors = {
            'info': 'text-blue-400',
            'success': 'text-green-400',
            'error': 'text-red-400',
            'warning': 'text-yellow-400',
            'stream': 'text-green-300'
        };
        
        logDiv.className += ' ' + (colors[type] || 'text-gray-400');
        logDiv.textContent = message;
        
        container.insertBefore(logDiv, container.firstChild);
        
        // Auto-scroll if enabled
        if (document.getElementById('autoScroll').checked) {
            container.scrollTop = 0;
        }
        
        // Limit log entries
        while (container.children.length > 200) {
            container.removeChild(container.lastChild);
        }
    }

    function clearLogs() {
        document.getElementById('logsContainer').innerHTML = '<div class="text-gray-500">Logs cleared...</div>';
    }

    function showResults() {
        document.getElementById('resultsPanel').classList.remove('hidden');
        addLog('✅ Scraping completed! Results panel shown', 'success');
    }

    // Auto-estimate images on URL change
    document.getElementById('storeUrl').addEventListener('blur', async function() {
        const url = this.value.trim();
        if (!url) return;
        document.getElementById('imageCount').textContent = '🔍 Estimating images...';
        try {
            const response = await fetch(`/api/estimate_images?url=${encodeURIComponent(url)}`);
            const result = await response.json();
            if (result.error) {
                document.getElementById('imageCount').textContent = '❌ Error: ' + result.error;
            } else {
                document.getElementById('imageCount').textContent = `📸 ~${result.image_count} images available`;
                if (result.is_store || result.is_search) {
                    document.getElementById('imageCount').textContent += ` across ${result.product_count} products`;
                    document.getElementById('maxProducts').max = result.product_count;
                }
            }
        } catch (error) {
            document.getElementById('imageCount').textContent = '⚠️ Estimation failed: ' + error.message;
        }
    });
    </script>
</body>
</html>
    """, rembg_available=REMBG_AVAILABLE)

@app.route('/api/start', methods=['POST'])
def api_start():
    """Start scraping job"""
    store_url = request.form.get('store_url', '').strip()
    if not store_url:
        return jsonify({'error': 'Store URL required'}), 400
    
    if not store_url.startswith('http'):
        store_url = 'https://' + store_url
    
    # Create job
    job_id = uuid.uuid4().hex[:12]
    job_folder = DOWNLOADS_DIR / job_id
    job_folder.mkdir(exist_ok=True)
    
    options = {
        'max_workers': min(int(request.form.get('max_workers', 8)), 15),
        'ignore_ssl': request.form.get('ignore_ssl') == 'true',
        'max_products': int(request.form.get('max_products', 30))
    }
    
    meta = {
        'job_id': job_id,
        'store_url': store_url,
        'folder': str(job_folder),
        'options': options,
        'state': 'pending',
        'stats': {'products': 0, 'images': 0, 'downloaded': 0, 'errors': 0},
        'logs': [],
        'images': [],
        'avg_time': None,
        'created': time.time()
    }
    
    with JOBS_LOCK:
        JOBS[job_id] = meta
        STOP_FLAGS[job_id] = False
    
    # Start scraping in background
    threading.Thread(target=lambda: run_scraping_job(job_id), daemon=True).start()
    
    return jsonify({'job_id': job_id})

@app.route('/api/stop/<job_id>', methods=['POST'])
def api_stop():
    """Stop scraping job"""
    if job_id not in JOBS:
        return jsonify({'error': 'Job not found'}), 404
    
    STOP_FLAGS[job_id] = True
    log_message(job_id, "Stop signal received", "warning")
    
    return jsonify({'status': 'stopping'})

@app.route('/api/remove_background/<job_id>', methods=['POST'])
def api_remove_background(job_id):
    """Remove background from selected images."""
    if job_id not in JOBS:
        return jsonify({'error': 'Job not found'}), 404
    
    data = request.get_json()
    if not data or not data.get('filenames'):
        return jsonify({'error': 'No filenames provided'}), 400
    
    try:
        threading.Thread(
            target=lambda: process_background_removal_job(job_id, data['filenames']),
            daemon=True
        ).start()
        return jsonify({'status': 'Background removal started'})
    except Exception as e:
        log_message(job_id, f"Failed to start background removal: {str(e)}", "error")
        return jsonify({'error': f'Failed to start: {str(e)}'}), 500

@app.route('/api/status/<job_id>')
def api_status(job_id):
    """Get job status with logs"""
    with JOBS_LOCK:
        meta = JOBS.get(job_id)
        if not meta:
            return jsonify({'error': 'Job not found'}), 404
        
        return jsonify({
            'state': meta['state'],
            'stats': meta['stats'],
            'logs': meta['logs'][:50],  # Last 50 log entries
            'avg_time': meta['avg_time']
        })

@app.route('/api/estimate_images')
def api_estimate_images():
    """Estimate number of images for a store, item, or search URL"""
    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({'error': 'URL required'}), 400
    if not url.startswith('http'):
        url = 'https://' + url
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5"
        }
        if '/itm/' in url:
            images = scrape_product_images('temp_job', url)
            return jsonify({
                'is_store': False,
                'is_search': False,
                'image_count': len(images),
                'product_count': 1
            })
        else:
            resp = session.get(url, verify=False, timeout=15, headers=headers)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.content, "html.parser")
            count_elem = soup.find('span', class_='rcnt') or soup.find('h1', class_='srp-controls__count-heading')
            total_products = 0
            if count_elem:
                count_text = count_elem.text.replace(',', '').replace(' results', '').replace(' result', '').strip()
                try:
                    total_products = int(count_text)
                except ValueError:
                    total_products = 0
            if total_products == 0:
                return jsonify({'error': 'No products found in store or search results'})
            item_links = soup.find_all('a', class_='s-item__link')[:5]
            image_count = 0
            for link in item_links:
                href = link.get('href')
                if href and '/itm/' in href:
                    images = scrape_product_images('temp_job', href.split('?')[0])
                    image_count += len(images)
            avg_images = image_count / max(1, len(item_links))
            estimated_images = int(avg_images * total_products)
            is_search = '/sch/i.html' in url
            return jsonify({
                'is_store': not is_search,
                'is_search': is_search,
                'image_count': estimated_images,
                'product_count': total_products
            })
    except Exception as e:
        print(f"Image estimation failed for URL {url}: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/view/<job_id>')
def view_results(job_id):
    """View detailed results with processing and upload options"""
    if job_id not in JOBS:
        return "Job not found", 404
    
    meta = JOBS[job_id]
    page = int(request.args.get('page', 1))
    per_page = 24
    
    images = [img for img in meta['images'] if img.get('filename')]
    total_pages = max(1, (len(images) + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    
    start = (page - 1) * per_page
    end = start + per_page
    
    return render_template_string("""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Gallery - eBay Scraper</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>body { font-family: 'Inter', sans-serif; }</style>
</head>
<body class="bg-gray-50">
    <div class="min-h-screen p-6">
        <div class="max-w-7xl mx-auto">
            <!-- Header -->
            <div class="flex justify-between items-center mb-8">
                <div>
                    <h1 class="text-3xl font-bold text-gray-900">Image Gallery</h1>
                    <p class="text-gray-600">Job: {{ job_id }} | {{ meta.store_url }}</p>
                    <div class="text-sm text-gray-500 mt-1">
                        Downloaded: {{ meta.stats.downloaded }}/{{ meta.stats.images }} images
                        {% if meta.avg_time %} | Avg: {{ "%.2f"|format(meta.avg_time) }}s{% endif %}
                    </div>
                </div>
                <div class="flex gap-3">
                    <a href="/" class="bg-gray-600 text-white px-4 py-2 rounded-md">← Back to Scraper</a>
                    <a href="/download/{{ job_id }}.zip" class="bg-blue-600 text-white px-4 py-2 rounded-md">Download ZIP</a>
                    {% if meta.state in ['bg_removed', 'bg_generated'] %}
                    <a href="/download/{{ job_id }}_bg_removed.zip" class="bg-green-600 text-white px-4 py-2 rounded-md">Download BG-Removed ZIP</a>
                    {% endif %}
                    {% if meta.state == 'bg_generated' %}
                    <a href="/download/{{ job_id }}_bg_generated.zip" class="bg-purple-600 text-white px-4 py-2 rounded-md">Download BG-Generated ZIP</a>
                    {% endif %}
                </div>
            </div>

            <!-- Processing Panel -->
            <div class="bg-white rounded-lg shadow p-6 mb-8">
                <h2 class="text-xl font-semibold mb-4">Image Processing</h2>
                <div class="grid grid-cols-1 md:grid-cols-5 gap-4 mb-4">
                    <!-- Image Upload -->
                    <div>
                        <label class="block text-sm font-medium mb-1">Upload Image</label>
                        <input id="imageUpload" type="file" accept="image/*" multiple
                               class="w-full px-3 py-2 border rounded-md">
                    </div>
                    {% if rembg_available %}
                    <div>
                        <label class="block text-sm font-medium mb-1">Format</label>
                        <select id="format" class="w-full px-3 py-2 border rounded-md">
                            <option value="PNG">PNG</option>
                            <option value="JPG">JPG</option>
                            <option value="WEBP">WEBP</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-sm font-medium mb-1">Quality</label>
                        <input id="quality" type="range" min="50" max="95" value="85" class="w-full">
                        <div class="text-xs text-center">85%</div>
                    </div>
                    <div>
                        <label class="block text-sm font-medium mb-1">Platform</label>
                        <select id="platform" class="w-full px-3 py-2 border rounded-md">
                            <option value="none">Custom</option>
                            <option value="amazon">Amazon</option>
                            <option value="ebay">eBay</option>
                            <option value="shopify">Shopify</option>
                        </select>
                    </div>
                    {% endif %}
                    <div>
                        <label class="block text-sm font-medium mb-1">Background Type</label>
                        <select id="bgType" class="w-full px-3 py-2 border rounded-md">
                            <option value="white">White (Amazon)</option>
                            <option value="lifestyle">Lifestyle (Shopify)</option>
                            <option value="studio">Studio (Shopify)</option>
                        </select>
                    </div>
                    {% if rembg_available %}
                    <div class="space-y-2">
                        <label class="flex items-center text-sm">
                            <input id="removeBg" type="checkbox" class="mr-2" checked>
                            Remove Background
                        </label>
                        <label class="flex items-center text-sm">
                            <input id="upscale" type="checkbox" class="mr-2">
                            Upscale 2x
                        </label>
                        <label class="flex items-center text-sm">
                            <input id="enhance" type="checkbox" class="mr-2">
                            Sharpen
                        </label>
                    </div>
                    {% endif %}
                </div>
                <div class="flex gap-3 items-center">
                    <button onclick="uploadImages()" class="bg-blue-600 text-white px-6 py-2 rounded-md hover:bg-blue-700">
                        Upload Images
                    </button>
                    {% if rembg_available %}
                    <button onclick="processSelected()" class="bg-indigo-600 text-white px-6 py-2 rounded-md hover:bg-indigo-700">
                        Process Selected
                    </button>
                    {% endif %}
                    <button onclick="removeBackground()" class="bg-green-600 text-white px-6 py-2 rounded-md hover:bg-green-700">
                        Remove Background
                    </button>
                    <button onclick="generateBackground()" class="bg-purple-600 text-white px-6 py-2 rounded-md hover:bg-purple-700">
                        Generate Background
                    </button>
                    <button onclick="selectAll()" class="bg-gray-600 text-white px-4 py-2 rounded-md">
                        Select All
                    </button>
                    <div id="processStatus" class="text-sm text-gray-600"></div>
                </div>
            </div>

            <!-- Image Grid -->
            <div class="bg-white rounded-lg shadow p-6">
                <div class="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-4 mb-6">
                    {% for img in page_images %}
                    <div class="border rounded-lg overflow-hidden bg-gray-50 relative group">
                        <div class="absolute top-2 left-2 z-10">
                            <input type="checkbox" class="image-select w-4 h-4" data-filename="{{ img.filename }}">
                        </div>
                        {% if img.filename %}
                        <img src="/images/{{ job_id }}/{{ img.filename }}" 
                             class="w-full h-32 object-contain bg-white cursor-pointer"
                             onclick="openImageModal('{{ img.url or '/images/' + job_id + '/' + img.filename }}', '{{ img.filename }}')"
                             onerror="this.style.display='none'; this.nextElementSibling.style.display='block';">
                        <div class="w-full h-32 flex items-center justify-center text-gray-400 hidden">
                            Failed to Load
                        </div>
                        {% else %}
                        <div class="w-full h-32 flex items-center justify-center text-red-400">
                            Download Failed
                        </div>
                        {% endif %}
                        <div class="p-2 text-xs bg-white">
                            <div class="text-blue-600 truncate">
                                <a href="{{ img.url or '/images/' + job_id + '/' + img.filename }}" target="_blank" class="hover:underline">Source</a>
                            </div>
                            {% if img.elapsed %}
                            <div class="text-gray-500">{{ "%.2f"|format(img.elapsed) }}s</div>
                            {% endif %}
                            {% if img.error %}
                            <div class="text-red-500 truncate">{{ img.error }}</div>
                            {% endif %}
                            {% if img.processed_filename %}
                            <div class="text-green-600 text-xs">✓ Processed</div>
                            {% endif %}
                            {% if img.bg_removed_filename %}
                            <div class="text-green-600 text-xs">✓ Background Removed</div>
                            {% endif %}
                            {% if img.bg_generated_filename %}
                            <div class="text-purple-600 text-xs">✓ Background Generated</div>
                            {% endif %}
                        </div>
                    </div>
                    {% endfor %}
                </div>
                {% if total_pages > 1 %}
                <div class="flex justify-center items-center gap-4">
                    {% if page > 1 %}
                    <a href="/view/{{ job_id }}?page={{ page - 1 }}" 
                       class="px-4 py-2 bg-gray-200 rounded hover:bg-gray-300">← Previous</a>
                    {% endif %}
                    <span class="text-gray-600">Page {{ page }} of {{ total_pages }}</span>
                    {% if page < total_pages %}
                    <a href="/view/{{ job_id }}?page={{ page + 1 }}" 
                       class="px-4 py-2 bg-gray-200 rounded hover:bg-gray-300">Next →</a>
                    {% endif %}
                </div>
                {% endif %}
            </div>

            <!-- Background Removed Images Panel -->
            <div id="bgRemovedPanel" class="mt-8 {% if meta.state != 'bg_removed' and meta.state != 'bg_generated' %}hidden{% endif %}">
                <div class="bg-white rounded-lg shadow p-6">
                    <h2 class="text-xl font-semibold mb-4">📸 Background Removed Images</h2>
                    <div class="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-4">
                        {% for img in page_images %}
                        {% if img.bg_removed_filename %}
                        <div class="border rounded-lg overflow-hidden bg-gray-50">
                            <img src="/images/{{ job_id }}/bg_removed/{{ img.bg_removed_filename }}" 
                                 class="w-full h-32 object-contain bg-white cursor-pointer"
                                 onclick="openImageModal('/images/{{ job_id }}/bg_removed/{{ img.bg_removed_filename }}', '{{ img.bg_removed_filename }}')">
                            <div class="p-2 text-xs bg-white">
                                <div class="text-green-600">{{ img.bg_removed_filename }}</div>
                            </div>
                        </div>
                        {% endif %}
                        {% endfor %}
                    </div>
                </div>
            </div>

            <!-- Background Generated Images Panel -->
            <div id="bgGeneratedPanel" class="mt-8 {% if meta.state != 'bg_generated' %}hidden{% endif %}">
                <div class="bg-white rounded-lg shadow p-6">
                    <h2 class="text-xl font-semibold mb-4">🎨 Background Generated Images</h2>
                    <div class="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-4">
                        {% for img in page_images %}
                        {% if img.bg_generated_filename %}
                        <div class="border rounded-lg overflow-hidden bg-gray-50">
                            <img src="/images/{{ job_id }}/bg_generated/{{ img.bg_generated_filename }}" 
                                 class="w-full h-32 object-contain bg-white cursor-pointer"
                                 onclick="openImageModal('/images/{{ job_id }}/bg_generated/{{ img.bg_generated_filename }}', '{{ img.bg_generated_filename }}')">
                            <div class="p-2 text-xs bg-white">
                                <div class="text-purple-600">{{ img.bg_generated_filename }}</div>
                            </div>
                        </div>
                        {% endif %}
                        {% endfor %}
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- Image Modal -->
    <div id="imageModal" class="fixed inset-0 bg-black bg-opacity-75 hidden z-50" onclick="closeImageModal()">
        <div class="flex items-center justify-center h-full p-4">
            <div class="bg-white rounded-lg max-w-4xl max-h-full overflow-auto">
                <div class="p-4">
                    <div class="flex justify-between items-center mb-4">
                        <h3 id="modalTitle" class="font-semibold"></h3>
                        <button onclick="closeImageModal()" class="text-gray-500 hover:text-gray-700 text-xl">×</button>
                    </div>
                    <img id="modalImage" src="" class="max-w-full h-auto">
                    <div class="mt-4">
                        <a id="modalSource" href="" target="_blank" class="text-blue-600 hover:underline">View Original Source</a>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script>
    function selectAll() {
        const checkboxes = document.querySelectorAll('.image-select');
        const allChecked = Array.from(checkboxes).every(cb => cb.checked);
        checkboxes.forEach(cb => cb.checked = !allChecked);
    }

    function openImageModal(sourceUrl, filename) {
        document.getElementById('modalTitle').textContent = filename;
        document.getElementById('modalImage').src = sourceUrl;
        document.getElementById('modalSource').href = sourceUrl;
        document.getElementById('imageModal').classList.remove('hidden');
    }

    function closeImageModal() {
        document.getElementById('imageModal').classList.add('hidden');
    }

    async function uploadImages() {
        const input = document.getElementById('imageUpload');
        if (!input.files.length) {
            alert('Please select at least one image to upload');
            return;
        }

        const formData = new FormData();
        for (const file of input.files) {
            formData.append('images', file);
        }

        try {
            document.getElementById('processStatus').textContent = 'Uploading images...';
            const response = await fetch('/api/upload_image/{{ job_id }}', {
                method: 'POST',
                body: formData
            });
            const result = await response.json();
            if (result.error) {
                alert(result.error);
                document.getElementById('processStatus').textContent = '';
                return;
            }
            document.getElementById('processStatus').textContent = '✅ Images uploaded!';
            setTimeout(() => window.location.reload(), 1000);
        } catch (error) {
            console.error('Upload error:', error);
            alert('Image upload failed: ' + error.message);
            document.getElementById('processStatus').textContent = '';
        }
    }

    async function processSelected() {
        const selected = Array.from(document.querySelectorAll('.image-select:checked'))
                              .map(cb => cb.dataset.filename)
                              .filter(Boolean);
        if (selected.length === 0) {
            alert('Please select at least one image to process');
            return;
        }
        const options = {
            remove_bg: document.getElementById('removeBg').checked,
            upscale: document.getElementById('upscale').checked,
            enhance: document.getElementById('enhance').checked,
            format: document.getElementById('format').value,
            quality: parseInt(document.getElementById('quality').value),
            platform: document.getElementById('platform').value,
            brightness: 1.0,
            contrast: 1.0,
            workers: 4
        };
        try {
            document.getElementById('processStatus').textContent = 'Starting processing...';
            const response = await fetch('/api/process/{{ job_id }}', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ filenames: selected, options })
            });
            const result = await response.json();
            if (result.error) {
                alert(result.error);
                return;
            }
            document.getElementById('processStatus').textContent = 'Processing in progress...';
            pollProcessing();
        } catch (error) {
            console.error('Processing error:', error);
            alert('Processing failed: ' + error.message);
            document.getElementById('processStatus').textContent = '';
        }
    }

    async function removeBackground() {
        const selected = Array.from(document.querySelectorAll('.image-select:checked'))
                              .map(cb => cb.dataset.filename)
                              .filter(Boolean);
        if (selected.length === 0) {
            alert('Please select at least one image to process');
            return;
        }
        try {
            document.getElementById('processStatus').textContent = 'Starting background removal...';
            const response = await fetch('/api/remove_background/{{ job_id }}', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ filenames: selected })
            });
            const result = await response.json();
            if (result.error) {
                alert(result.error);
                return;
            }
            document.getElementById('processStatus').textContent = 'Background removal in progress...';
            pollBackgroundRemoval();
        } catch (error) {
            console.error('Background removal error:', error);
            alert('Background removal failed: ' + error.message);
            document.getElementById('processStatus').textContent = '';
        }
    }

    async function generateBackground() {
        const selected = Array.from(document.querySelectorAll('.image-select:checked'))
                              .map(cb => cb.dataset.filename)
                              .filter(Boolean);
        if (selected.length === 0) {
            alert('Please select at least one image to process');
            return;
        }
        const bgType = document.getElementById('bgType').value;
        try {
            console.log('Starting background generation for:', selected, 'with type:', bgType);
            document.getElementById('processStatus').textContent = 'Starting background generation...';
            const response = await fetch('/api/generate_background/{{ job_id }}', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ filenames: selected, bg_type: bgType })
            });
            const result = await response.json();
            if (result.error) {
                console.error('Background generation error:', result.error);
                alert(result.error);
                return;
            }
            console.log('Background generation started:', result);
            document.getElementById('processStatus').textContent = 'Background generation in progress...';
            pollBackgroundGeneration();
        } catch (error) {
            console.error('Background generation fetch error:', error);
            alert('Background generation failed: ' + error.message);
            document.getElementById('processStatus').textContent = '';
        }
    }

    async function pollBackgroundRemoval() {
        while (true) {
            try {
                const response = await fetch('/api/status/{{ job_id }}');
                const status = await response.json();
                console.log('Background removal status:', status);
                if (status.state === 'bg_removed') {
                    document.getElementById('processStatus').textContent = '✅ Background removal completed!';
                    document.getElementById('bgRemovedPanel').classList.remove('hidden');
                    setTimeout(() => window.location.reload(), 2000);
                    break;
                } else if (status.state === 'failed') {
                    document.getElementById('processStatus').textContent = '❌ Background removal failed';
                    break;
                }
                await new Promise(resolve => setTimeout(resolve, 1000));
            } catch (error) {
                console.error('Polling error:', error);
                document.getElementById('processStatus').textContent = '⚠️ Polling error';
                break;
            }
        }
    }

    async function pollBackgroundGeneration() {
        while (true) {
            try {
                const response = await fetch('/api/status/{{ job_id }}');
                const status = await response.json();
                console.log('Background removal status:', status);
                if (status.state === 'bg_removed') {
                    document.getElementById('processStatus').textContent = '✅ Background removal completed!';
                    document.getElementById('bgRemovedPanel').classList.remove('hidden');
                    setTimeout(() => window.location.reload(), 2000);
                    break;
                } else if (status.state === 'failed') {
                    document.getElementById('processStatus').textContent = '❌ Background removal failed';
                    break;
                }
                await new Promise(resolve => setTimeout(resolve, 1000));
            } catch (error) {
                console.error('Polling error:', error);
                document.getElementById('processStatus').textContent = '⚠️ Polling error';
                break;
            }
        }
    }

    async function pollBackgroundGeneration() {
        while (true) {
            try {
                const response = await fetch('/api/status/{{ job_id }}');
                const status = await response.json();
                console.log('Background generation status:', status);
                if (status.state === 'bg_generated') {
                    document.getElementById('processStatus').textContent = '✅ Background generation completed!';
                    document.getElementById('bgGeneratedPanel').classList.remove('hidden');
                    setTimeout(() => window.location.reload(), 2000);
                    break;
                } else if (status.state === 'failed') {
                    document.getElementById('processStatus').textContent = '❌ Background generation failed';
                    break;
                }
                await new Promise(resolve => setTimeout(resolve, 1000));
            } catch (error) {
                console.error('Polling error:', error);
                document.getElementById('processStatus').textContent = '⚠️ Polling error';
                break;
            }
        }
    }

    async function pollProcessing() {
        while (true) {
            try {
                const response = await fetch('/api/status/{{ job_id }}');
                const status = await response.json();
                console.log('Processing status:', status);
                if (status.state === 'processed') {
                    document.getElementById('processStatus').textContent = '✅ Processing completed!';
                    setTimeout(() => window.location.reload(), 2000);
                    break;
                } else if (status.state === 'failed') {
                    document.getElementById('processStatus').textContent = '❌ Processing failed';
                    break;
                }
                await new Promise(resolve => setTimeout(resolve, 1000));
            } catch (error) {
                console.error('Polling error:', error);
                document.getElementById('processStatus').textContent = '⚠️ Polling error';
                break;
            }
        }
    }

    // Quality slider update
    document.getElementById('quality').addEventListener('input', function() {
        this.nextElementSibling.textContent = this.value + '%';
    });
    </script>
</body>
</html>
    """, 
    job_id=job_id, 
    meta=meta, 
    page_images=images[start:end],
    page=page,
    total_pages=total_pages,
    rembg_available=REMBG_AVAILABLE
    )

@app.route('/api/upload_image/<job_id>', methods=['POST'])
def api_upload_image(job_id):
    """Upload images to a job folder and update metadata."""
    if job_id not in JOBS:
        return jsonify({'error': 'Job not found'}), 404
    
    if 'images' not in request.files:
        return jsonify({'error': 'No images uploaded'}), 400
    
    meta = JOBS[job_id]
    save_dir = Path(meta['folder'])
    save_dir.mkdir(exist_ok=True)
    
    uploaded_filenames = []
    for file in request.files.getlist('images'):
        try:
            filename = f"uploaded_{uuid.uuid4().hex[:8]}{Path(file.filename).suffix}"
            file_path = save_dir / filename
            file.save(file_path)
            
            # Create thumbnail for faster preview
            try:
                with Image.open(file_path) as img:
                    img.thumbnail((300, 300), Image.LANCZOS)
                    img.save(file_path, quality=85, optimize=True)
            except Exception as e:
                log_message(job_id, f"Failed to create thumbnail for {filename}: {str(e)}", "error")
            
            # Update metadata
            with JOBS_LOCK:
                meta['images'].append({
                    'url': None,  # No source URL for uploaded images
                    'filename': filename,
                    'elapsed': 0,
                    'error': None,
                    'processed_filename': None,
                    'bg_removed_filename': None,
                    'bg_generated_filename': None
                })
                meta['stats']['images'] += 1
                meta['stats']['downloaded'] += 1
                uploaded_filenames.append(filename)
            
            log_message(job_id, f"Uploaded image: {filename}", "success")
        except Exception as e:
            log_message(job_id, f"Failed to upload {file.filename}: {str(e)}", "error")
            continue
    
    if uploaded_filenames:
        # Update ZIP archive to include new images
        try:
            zip_path = DOWNLOADS_DIR / f"{job_id}.zip"
            shutil.make_archive(str(zip_path.with_suffix('')), 'zip', str(save_dir))
            log_message(job_id, f"Updated ZIP with {len(uploaded_filenames)} new images", "success")
        except Exception as e:
            log_message(job_id, f"ZIP update failed: {str(e)}", "error")
    
    return jsonify({'status': 'Images uploaded', 'filenames': uploaded_filenames})

@app.route('/api/process/<job_id>', methods=['POST'])
def api_process(job_id):
    """Process selected images"""
    if job_id not in JOBS:
        return jsonify({'error': 'Job not found'}), 404
    
    data = request.get_json()
    if not data or not data.get('filenames'):
        return jsonify({'error': 'No filenames provided'}), 400
    
    threading.Thread(
        target=lambda: process_images_job(job_id, data['filenames'], data.get('options', {})),
        daemon=True
    ).start()
    
    return jsonify({'status': 'Processing started'})

@app.route('/images/<job_id>/<path:filename>')
def serve_image(job_id, filename):
    """Serve images from job folder or subfolders."""
    if job_id not in JOBS:
        return "Job not found", 404
    
    job_folder = Path(JOBS[job_id]['folder'])
    
    for folder in [job_folder, job_folder / 'processed', job_folder / 'bg_removed', job_folder / 'bg_generated']:
        file_path = folder / filename
        if file_path.exists():
            return send_from_directory(str(folder), filename)
    
    return "Image not found", 404

@app.route('/api/generate_background/<job_id>', methods=['POST'])
def api_generate_background(job_id):
    """Generate background for selected images."""
    if job_id not in JOBS:
        return jsonify({'error': 'Job not found'}), 404
    
    data = request.get_json()
    if not data or not data.get('filenames') or not data.get('bg_type'):
        return jsonify({'error': 'No filenames or background type provided'}), 400
    
    bg_type = data['bg_type']
    if bg_type not in ['white', 'lifestyle', 'studio']:
        return jsonify({'error': 'Invalid background type'}), 400
    
    try:
        threading.Thread(
            target=lambda: process_background_generation_job(job_id, data['filenames'], bg_type),
            daemon=True
        ).start()
        return jsonify({'status': 'Background generation started'})
    except Exception as e:
        log_message(job_id, f"Failed to start background generation: {str(e)}", "error")
        return jsonify({'error': f'Failed to start: {str(e)}'}), 500

@app.route('/suggest-scene', methods=['POST'])
def suggest_scene():
    if not OPENAI_AVAILABLE or not CLIP_AVAILABLE:
        return jsonify({'error': 'AI dependencies not available'}), 503

    data = request.get_json()
    category = data.get('category')
    store_url = data.get('store_url')
    if not category or not store_url:
        return jsonify({'error': 'Category and store_url required'}), 400

    try:
        # Get brand description
        resp = requests.get(store_url, verify=False, timeout=10)
        soup = BeautifulSoup(resp.content, "html.parser")
        brand_description = ""
        # Try meta description
        meta_desc = soup.find('meta', attrs={'name': 'description'})
        if meta_desc:
            brand_description = meta_desc['content']
        else:
            # Try about section or title
            title = soup.title.string if soup.title else ""
            brand_description = title
        if not brand_description:
            brand_description = store_url.split('//')[1].split('.')[0].capitalize() + " brand"

        # Generate 10 prompts with GPT
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        if not client.api_key:
            return jsonify({'error': 'OpenAI API key not set'}), 500

        prompt = f"Generate 10 detailed prompt suggestions for lifestyle background scenes for a product photography of {category}. Make them diverse and creative."
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are a creative AI for generating background prompts for product photos."},
                {"role": "user", "content": prompt}
            ]
        )
        content = response.choices[0].message.content
        # Parse to list, assume numbered
        prompts = []
        for line in content.split('\n'):
            if line.strip().startswith(tuple(str(i) + '.' for i in range(1,11))):
                prompts.append(line.split('.',1)[1].strip())
        if len(prompts) < 5:
            return jsonify({'error': 'Failed to generate enough prompts'}), 500

        # Now rank with CLIP
        model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32")
        processor = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")
        # Embed brand description
        inputs = processor(text=[brand_description], images=None, return_tensors="pt", padding=True)
        with torch.no_grad():
            brand_embedding = model.get_text_features(**inputs)
        # Embed prompts
        inputs = processor(text=prompts, images=None, return_tensors="pt", padding=True)
        with torch.no_grad():
            prompt_embeddings = model.get_text_features(**inputs)
        # Compute similarities
        similarities = torch.nn.functional.cosine_similarity(brand_embedding, prompt_embeddings, dim=1)
        # Get top 5 indices
        top_indices = similarities.topk(5).indices.tolist()
        top_prompts = [prompts[i] for i in top_indices]

        return jsonify({'suggestions': top_prompts})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/download/<filename>')
def download_file(filename):
    """Download ZIP files"""
    file_path = DOWNLOADS_DIR / filename
    if not file_path.exists():
        return "File not found", 404
    
    return send_from_directory(str(DOWNLOADS_DIR), filename, as_attachment=True)

# ----------------------- Background Cleanup -----------------------

def cleanup_old_jobs():
    """Clean up expired jobs."""
    while True:
        try:
            with JOBS_LOCK:
                current_time = time.time()
                expired = [jid for jid, meta in JOBS.items() 
                          if current_time - meta['created'] > 86400]  # 24 hours
                
                for job_id in expired:
                    job_folder = Path(JOBS[job_id]['folder'])
                    if job_folder.exists():
                        shutil.rmtree(job_folder, ignore_errors=True)
                    
                    for suffix in ['', '_processed', '_bg_removed', '_bg_generated']:
                        zip_file = DOWNLOADS_DIR / f"{job_id}{suffix}.zip"
                        if zip_file.exists():
                            zip_file.unlink()
                    
                    del JOBS[job_id]
                    if job_id in STOP_FLAGS:
                        del STOP_FLAGS[job_id]
        except Exception as e:
            print(f"Cleanup error: {e}")
        
        time.sleep(3600)

# ----------------------- Main Application -----------------------

if __name__ == "__main__":
    # This is for local development only
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)






