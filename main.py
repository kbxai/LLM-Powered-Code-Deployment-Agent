import os
import time
import secrets
import base64
import traceback
import re
import json
from typing import List, Optional, Dict, Tuple, Union

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel, HttpUrl, Field

from openai import OpenAI
from dotenv import load_dotenv
from github import Github, GithubException, UnknownObjectException
import requests

load_dotenv()

class Config:
    LLM_MODEL: str = os.getenv("LLM_MODEL", "openai/gpt-4o")
    GEMINI_MODEL: str = "gemini-3.8-flash" if os.getenv("GEMINI_MODEL") == "gemini-2.5-flash" else os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
    MY_SECRET: Optional[str] = os.getenv("MY_SECRET") or None  # no default: builds are refused until it is set
    AIPIPE_TOKEN: Optional[str] = os.getenv("AIPIPE_TOKEN")
    GEMINI_API_KEY: Optional[str] = os.getenv("GEMINI_API_KEY")
    GITHUB_TOKEN: Optional[str] = os.getenv("GITHUB_TOKEN")
    GITHUB_USERNAME: Optional[str] = os.getenv("GITHUB_USERNAME")
    DEPLOYMENT_TIMEOUT: int = int(os.getenv("DEPLOYMENT_TIMEOUT", 180))
    MAX_ATTACHMENT_SIZE: int = 10 * 1024 * 1024

config = Config()

class Attachment(BaseModel):
    name: str
    url: str

class BuildRequest(BaseModel):
    email: str
    secret: str
    task: str
    round: int
    nonce: str
    brief: str
    evaluation_url: HttpUrl
    attachments: Optional[List[Attachment]] = Field(default=None)
    checks: Optional[List[str]] = Field(default=None)

app = FastAPI()

@app.on_event("startup")
def startup_event():
    print("--- Performing startup validation of environment variables ---")
    if not config.AIPIPE_TOKEN and not config.GEMINI_API_KEY:
        print("CRITICAL WARNING: Neither AIPIPE_TOKEN nor GEMINI_API_KEY is set. Code generation will fail.")
    if not config.GITHUB_TOKEN:
        print("CRITICAL WARNING: GITHUB_TOKEN is not set.")
    if not config.GITHUB_USERNAME:
        print("CRITICAL WARNING: GITHUB_USERNAME is not set.")
    if not config.MY_SECRET:
        print("CRITICAL WARNING: MY_SECRET is not set. /api/build will refuse every request.")
    print("--- Startup validation complete. ---")

def sanitize_filename(filename: str) -> str:
    cleaned = filename.replace("..", "")
    return re.sub(r'[^a-zA-Z0-9_.-]', '_', cleaned)

def clean_json_response(raw_text: str) -> dict:
    text = raw_text.strip()
    if text.startswith("```json"):
        text = text.split("```json", 1)[1].rsplit("```", 1)[0].strip()
    elif text.startswith("```"):
        text = text.split("```", 1)[1].rsplit("```", 1)[0].strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start_idx = text.find("{")
        end_idx = text.rfind("}")
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            substring = text[start_idx:end_idx + 1]
            return json.loads(substring)
        raise

def call_llm(prompt: str) -> str:
    providers = []
    if config.AIPIPE_TOKEN:
        providers.append({
            "name": "AI Pipe",
            "client": OpenAI(base_url="https://aipipe.org/openrouter/v1", api_key=config.AIPIPE_TOKEN),
            "model": config.LLM_MODEL
        })
    if config.GEMINI_API_KEY:
        gemini_client = OpenAI(base_url="https://generativelanguage.googleapis.com/v1beta/openai/", api_key=config.GEMINI_API_KEY)
        gemini_models = [config.GEMINI_MODEL, "gemini-3.5-flash", "gemini-3.5-flash-lite"]
        seen_models = []
        for m in gemini_models:
            if m not in seen_models:
                seen_models.append(m)
                providers.append({
                    "name": f"Gemini ({m})",
                    "client": gemini_client,
                    "model": m
                })

    if not providers:
        raise HTTPException(
            status_code=503,
            detail="Server configuration error: Neither AIPIPE_TOKEN nor GEMINI_API_KEY is configured."
        )

    last_error = None
    for p in providers:
        print(f"Calling LLM provider: {p['name']} with model: {p['model']}")
        for attempt in range(2):
            try:
                completion = p["client"].chat.completions.create(
                    model=p["model"],
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
                    timeout=120.0,
                    response_format={"type": "json_object"}
                )
                output = completion.choices[0].message.content
                if output:
                    return output.strip()
            except Exception as err:
                last_error = err
                print(f"Provider {p['name']} attempt {attempt + 1} failed: {err}")
                time.sleep(2)

    raise HTTPException(status_code=504, detail=f"All LLM providers failed. Last error: {last_error}")

def generate_code_from_brief(request_data: BuildRequest, existing_code_context: str = None) -> Tuple[Dict[str, str], Dict[str, bytes]]:
    attachments_content = ""
    binary_files_to_commit: Dict[str, bytes] = {}

    if request_data.attachments:
        for attachment in request_data.attachments:
            try:
                if attachment.url.startswith("data:"):
                    header, encoded = attachment.url.split(",", 1)
                    mime_type = header.split(";")[0].split(":")[1]
                    decoded_bytes = base64.b64decode(encoded)
                elif attachment.url.startswith("http"):
                    print(f"Downloading attachment from URL: {attachment.url}")
                    response = requests.get(attachment.url, timeout=30)
                    response.raise_for_status()
                    decoded_bytes = response.content
                    mime_type = response.headers.get("Content-Type", "application/octet-stream")
                else:
                    print(f"Unsupported attachment URL format: {attachment.url}")
                    continue

                if len(decoded_bytes) > config.MAX_ATTACHMENT_SIZE:
                    print(f"Attachment {attachment.name} too large. Skipping.")
                    continue

                safe_filename = sanitize_filename(attachment.name)
                binary_files_to_commit[safe_filename] = decoded_bytes

                if mime_type.startswith("image/"):
                    attachments_content += f"\n\n--- Attachment: `{safe_filename}` (Image file) ---\n"
                    attachments_content += f"Original URL: {attachment.url}\n"
                    attachments_content += "IMPORTANT: Use this URL as the default/fallback image in your generated code.\n"
                elif mime_type.startswith("text/") or mime_type in ("application/json", "application/javascript"):
                    try:
                        text_content = decoded_bytes.decode('utf-8', errors='ignore')
                        attachments_content += f"\n\n--- Attachment: `{safe_filename}` ---\n```\n{text_content}\n```"
                    except Exception:
                        attachments_content += f"\n\n--- Attachment: `{safe_filename}` (text decode failed) ---"
                else:
                    attachments_content += f"\n\n--- Attachment: `{safe_filename}` (Binary file saved to repo) ---"

            except Exception as e:
                print(f"Warning: Could not process attachment '{attachment.name}'. Error: {e}")
                traceback.print_exc()

    technical_requirements = ""
    if request_data.checks:
        technical_requirements += "\n\n**Technical Implementation Requirements (CRITICAL):**\n"
        for i, check in enumerate(request_data.checks):
            technical_requirements += f"{i+1}. The generated webpage MUST pass this JavaScript evaluation: `{check}`\n"

    existing_section = ""
    if existing_code_context:
        existing_section = f"**EXISTING CODE BASE:**\n{existing_code_context}\n"

    prompt_template = """
You are an elite software engineer. Your task is to generate the complete content for all files requested in the user's brief.

Analyze the user's brief, attachments, and technical requirements.

Your output MUST be a single, valid JSON object.
The keys of the JSON object must be the full filenames (e.g., "index.html", "data.json", "style.css").
The values must be the complete, raw string content for each file.

Do not include any explanations, comments, or markdown outside of the final JSON object.
For external libraries in HTML, use public CDNs.

{existing_code_section}

**USER'S BRIEF:** "{brief}"

{attachments}

**CRITICAL INSTRUCTIONS FOR ATTACHMENTS:**
- If image attachments are provided with URLs, use those EXACT URLs as the default/fallback image source
- For captcha solvers: The page MUST support ?url=... parameter to load custom images
- If no ?url parameter is provided, use the attachment URL as default
- Ensure CORS handling for cross-origin images (use crossOrigin="anonymous")
- Use modern CDN libraries (e.g., Tesseract.js v4 from cdn.jsdelivr.net)
- For text attachments (CSV, JSON, Markdown): Fetch from relative path (e.g., fetch("data.csv")) unless ?url= parameter is provided
- If ?url= parameter is present, fetch from that URL; otherwise fetch from the local file in the repo root

**CRITICAL INSTRUCTIONS FOR FILE GENERATION:**
- Generate ALL files requested in the brief, including .txt, .json, .md, .svg, and .html.
- The 'index.html' file should link to all other files you generate.
- DO NOT generate a 'README.md' or 'LICENSE' file. They will be added automatically.

{tech_reqs}
"""

    final_prompt = prompt_template.format(
        existing_code_section=existing_section,
        brief=request_data.brief,
        attachments=attachments_content,
        tech_reqs=technical_requirements,
    )

    try:
        raw_output = call_llm(final_prompt)
        generated_files_dict = clean_json_response(raw_output)
        return generated_files_dict, binary_files_to_commit
    except Exception as e:
        print(f"LLM API call or JSON parsing failed: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=504, detail=f"LLM API call or JSON parsing failed: {e}")

def get_existing_file(repo, file_path):
    try:
        return repo.get_contents(file_path, ref=repo.default_branch)
    except UnknownObjectException:
        return None

def read_all_repo_code(repo) -> str:
    content_parts = []
    try:
        items = repo.get_contents("", ref=repo.default_branch)
        stack = list(items) if isinstance(items, list) else [items]
        count = 0
        while stack and count < 15:
            current = stack.pop(0)
            if current.type == "dir":
                sub_items = repo.get_contents(current.path, ref=repo.default_branch)
                stack.extend(sub_items if isinstance(sub_items, list) else [sub_items])
            elif current.type == "file":
                count += 1
                if current.name in ("README.md", "LICENSE"):
                    continue
                if current.name.endswith(('.html', '.js', '.css', '.json', '.txt', '.csv', '.svg', '.md')):
                    try:
                        text = current.decoded_content.decode("utf-8", errors="ignore")
                        content_parts.append(f"--- File: {current.path} ---\n```\n{text}\n```")
                    except Exception:
                        pass
    except Exception as e:
        print(f"Could not load full repo context: {e}")
    return "\n\n".join(content_parts)

def enable_github_pages(repo):
    owner = repo.owner.login
    repo_name = repo.name
    branch = repo.default_branch
    print(f"Attempting to enable GitHub Pages for {owner}/{repo_name} on branch {branch} (root).")

    try:
        create_pages = getattr(repo, "create_pages_site", None)
        if callable(create_pages):
            try:
                repo.create_pages_site(source_type="branch", source_branch=branch, source_path="/")
                print("GitHub Pages enabled via PyGithub.")
                return True
            except GithubException as e:
                print(f"PyGithub create_pages_site note: {e.status}")
    except Exception as e:
        print(f"PyGithub pages attempt: {e}")

    try:
        url = f"https://api.github.com/repos/{owner}/{repo_name}/pages"
        headers = {
            "Authorization": f"token {config.GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }
        payload = {"source": {"branch": branch, "path": "/"}}
        response = requests.post(url, json=payload, headers=headers, timeout=20)
        if response.status_code in (201, 204):
            print("GitHub Pages enabled via REST API.")
            return True
        if response.status_code == 422 and "already configured" in response.text.lower():
            print("GitHub Pages already configured.")
            return True
        print(f"REST API Pages response {response.status_code}: {response.text}")
    except requests.RequestException as e:
        print(f"REST API Pages enable error: {e}")

    return False

def verify_deployment(pages_url: str, timeout: int) -> bool:
    print(f"Verifying deployment at {pages_url}. Will wait up to {timeout} seconds.")
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            response = requests.get(pages_url, timeout=10)
            if response.status_code == 200:
                print("Deployment verified successfully (HTTP 200).")
                return True
        except requests.exceptions.RequestException:
            pass
        time.sleep(5)
    print("Error: Deployment verification timed out.")
    return False

def create_or_update_file(repo, file_path: str, commit_message: str, content: Union[str, bytes]):
    file = get_existing_file(repo, file_path)
    if file:
        if isinstance(content, str):
            existing_str = file.decoded_content.decode("utf-8", errors="ignore")
            if existing_str != content:
                repo.update_file(file.path, commit_message, content, file.sha, branch=repo.default_branch)
                print(f"Updated file: {file_path}")
            else:
                print(f"No change for file: {file_path}")
        else:
            if file.decoded_content != content:
                repo.update_file(file.path, commit_message, content, file.sha, branch=repo.default_branch)
                print(f"Updated binary file: {file_path}")
            else:
                print(f"No change for binary file: {file_path}")
    else:
        repo.create_file(file_path, commit_message, content, branch=repo.default_branch)
        print(f"Created file: {file_path}")

def sync_repo_files(repo, generated_files: dict, binary_files: dict, round_num: int = 1):
    for filename, content in generated_files.items():
        if not content:
            continue
        safe_path = filename.strip("/\\")
        create_or_update_file(repo, safe_path, f"Update {safe_path} (Round {round_num})", str(content))

    for filename, content_bytes in binary_files.items():
        safe_name = sanitize_filename(filename)
        if safe_name.endswith(('.csv', '.txt', '.md', '.json', '.js', '.xml', '.yaml', '.yml')):
            text_str = content_bytes.decode('utf-8', errors='ignore')
            create_or_update_file(repo, safe_name, f"Add attachment {safe_name}", text_str)
        else:
            create_or_update_file(repo, safe_name, f"Add binary asset {safe_name}", content_bytes)

def create_and_deploy(request_data: BuildRequest, generated_files: dict, binary_files: dict):
    g = Github(config.GITHUB_TOKEN)
    user = g.get_user()
    repo_name = request_data.task

    try:
        repo = user.create_repo(repo_name, auto_init=True, private=False)
        print(f"Created repository: {repo.full_name}")
    except GithubException as e:
        if getattr(e, "status", None) == 422:
            repo = g.get_repo(f"{user.login}/{repo_name}")
            print(f"Using existing repository: {repo.full_name}")
        else:
            raise e

    readme_content = f"""# {repo_name.replace('-', ' ').title()}

## Project Summary

This repository hosts a web application automatically generated by an LLM-powered code deployment agent.

**Brief:** "{request_data.brief}"

## Setup

1. Clone this repository
2. Open `index.html` in a web browser or deploy to any static hosting service
3. No build process required — this is a static HTML application

## Usage

Visit the live application at: https://{config.GITHUB_USERNAME}.github.io/{repo_name}/

The application implements the requirements specified in the brief above.

## Code Structure

- `index.html` — Main application file containing all HTML, CSS, and JavaScript
- `LICENSE` — MIT License
- Additional data files may be present depending on the task requirements

## License

This project is licensed under the MIT License. See the LICENSE file for details.
"""
    license_text = """MIT License

Copyright (c) 2025 TDS Student

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

    sync_repo_files(repo, generated_files, binary_files, round_num=1)
    create_or_update_file(repo, "README.md", "Create/Update README", readme_content)
    create_or_update_file(repo, "LICENSE", "Create/Update LICENSE", license_text)

    pages_enabled = enable_github_pages(repo)
    if not pages_enabled:
        print("Warning: GitHub Pages could not be confirmed automatically.")

    commit_sha = repo.get_branch(repo.default_branch).commit.sha
    pages_url = f"https://{config.GITHUB_USERNAME}.github.io/{repo_name}/"

    verified = verify_deployment(pages_url, config.DEPLOYMENT_TIMEOUT)
    if not verified:
        print(f"Warning: Deployment not verified at {pages_url} within timeout.")

    return repo.html_url, commit_sha, pages_url

def revise_and_deploy(request_data: BuildRequest, generated_files: dict, binary_files: dict):
    g = Github(config.GITHUB_TOKEN)
    user = g.get_user()
    repo_name = request_data.task

    try:
        repo = g.get_repo(f"{user.login}/{repo_name}")
    except UnknownObjectException:
        raise ValueError(f"Repository {repo_name} not found for revision.")

    readme_file = get_existing_file(repo, "README.md")
    existing_readme = readme_file.decoded_content.decode("utf-8") if readme_file else ""
    new_readme_content = f"{existing_readme}\n\n### Round {request_data.round} Update\n\n> {request_data.brief}"

    sync_repo_files(repo, generated_files, binary_files, round_num=request_data.round)
    create_or_update_file(repo, "README.md", f"Update README for Round {request_data.round}", new_readme_content)

    enable_github_pages(repo)

    commit_sha = repo.get_branch(repo.default_branch).commit.sha
    pages_url = f"https://{config.GITHUB_USERNAME}.github.io/{repo.name}/"

    verify_deployment(pages_url, config.DEPLOYMENT_TIMEOUT)
    return repo.html_url, commit_sha, pages_url

def notify_evaluation_server(payload: dict):
    url = str(payload.pop("evaluation_url"))
    for i, delay in enumerate([1, 2, 4, 8]):
        try:
            response = requests.post(url, json=payload, timeout=20)
            response.raise_for_status()
            print(f"Successfully notified evaluation server on attempt {i+1}.")
            return True
        except requests.exceptions.RequestException as e:
            print(f"Notification attempt {i+1} failed: {e}. Retrying in {delay}s...")
            if i < 3:
                time.sleep(delay)
    print("All notification attempts failed.")
    return False

def run_build_and_deploy_task(request_data: BuildRequest):
    print(f"Starting background task for '{request_data.task}', round {request_data.round}.")
    try:
        if request_data.round == 1:
            generated_files, binary_files = generate_code_from_brief(request_data)
            repo_url, commit_sha, pages_url = create_and_deploy(request_data, generated_files, binary_files)
        else:
            g = Github(config.GITHUB_TOKEN)
            user = g.get_user()
            repo = g.get_repo(f"{user.login}/{request_data.task}")

            existing_code_context = read_all_repo_code(repo)
            if not existing_code_context:
                file_content = get_existing_file(repo, "index.html")
                if file_content:
                    existing_code_context = file_content.decoded_content.decode("utf-8", errors="ignore")

            generated_files, binary_files = generate_code_from_brief(request_data, existing_code_context)
            repo_url, commit_sha, pages_url = revise_and_deploy(request_data, generated_files, binary_files)

        notification_payload = {
            "email": request_data.email,
            "task": request_data.task,
            "round": request_data.round,
            "nonce": request_data.nonce,
            "repo_url": repo_url,
            "commit_sha": commit_sha,
            "pages_url": pages_url,
            "evaluation_url": str(request_data.evaluation_url)
        }

        notified = notify_evaluation_server(notification_payload)
        if not notified:
            print(f"CRITICAL (background): Build for '{request_data.task}' succeeded, but FAILED to notify server.")

        print(f"Background task for '{request_data.task}' completed successfully!")

    except Exception as e:
        print(f"FATAL ERROR in background task for '{request_data.task}': {e}")
        traceback.print_exc()

@app.post("/api/build")
def handle_build_request(request_data: BuildRequest, background_tasks: BackgroundTasks):
    if not config.MY_SECRET:
        raise HTTPException(status_code=503, detail="Builds are disabled: the server has no secret configured.")
    if not secrets.compare_digest(request_data.secret.encode("utf-8"), config.MY_SECRET.encode("utf-8")):
        raise HTTPException(status_code=403, detail="Authentication failed: Invalid secret.")

    has_llm = bool(config.AIPIPE_TOKEN or config.GEMINI_API_KEY)
    if not (has_llm and config.GITHUB_TOKEN and config.GITHUB_USERNAME):
        raise HTTPException(
            status_code=503,
            detail="Server is not fully configured. Missing GITHUB_TOKEN/GITHUB_USERNAME or LLM credentials."
        )

    request_data.task = sanitize_filename(request_data.task)
    background_tasks.add_task(run_build_and_deploy_task, request_data)

    return {"status": "accepted", "message": "The build and deploy process has been started in the background."}

HERE = os.path.dirname(__file__)

def serve_page(name: str):
    path = os.path.join(HERE, name)
    if os.path.exists(path):
        return FileResponse(path)
    return HTMLResponse("<h2>LLM Code Deployer is running.</h2>", status_code=404)

@app.get("/", response_class=HTMLResponse)
async def root():
    """Public page: what the service does, how to call it and its limits."""
    return serve_page("index.html")

@app.get("/console", response_class=HTMLResponse)
async def console():
    """Operator console for manual builds. Every build still needs MY_SECRET."""
    return serve_page("console.html")

@app.get("/api/status")
async def status():
    return {
        "status": "online",
        "service": "LLM Code Deployer",
        "llm_providers": {
            "aipipe": bool(config.AIPIPE_TOKEN),
            "gemini": bool(config.GEMINI_API_KEY)
        },
        "github_configured": bool(config.GITHUB_TOKEN and config.GITHUB_USERNAME)
    }
