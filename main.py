import os
import time
import base64
import traceback
import re
from typing import List, Optional, Dict, Tuple

from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel, HttpUrl, Field

from openai import OpenAI
from dotenv import load_dotenv
from github import Github, GithubException, UnknownObjectException
import requests

# Load environment variables
load_dotenv()

class Config:
    LLM_MODEL: str = os.getenv("LLM_MODEL", "openai/gpt-4o")
    MY_SECRET: str = os.getenv("MY_SECRET", "my-super-secret-123")
    AIPIPE_TOKEN: str = os.getenv("AIPIPE_TOKEN")
    GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN")
    GITHUB_USERNAME: str = os.getenv("GITHUB_USERNAME")
    DEPLOYMENT_TIMEOUT: int = int(os.getenv("DEPLOYMENT_TIMEOUT", 180))
    MAX_ATTACHMENT_SIZE: int = 10 * 1024 * 1024

config = Config()

# Pydantic models
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
    if not config.AIPIPE_TOKEN: print("CRITICAL WARNING: AIPIPE_TOKEN is not set.")
    if not config.GITHUB_TOKEN: print("CRITICAL WARNING: GITHUB_TOKEN is not set.")
    if not config.GITHUB_USERNAME: print("CRITICAL WARNING: GITHUB_USERNAME is not set.")
    print("--- Startup validation complete. ---")

def sanitize_filename(filename: str) -> str:
    sanitized = filename.replace("..", "")
    return re.sub(r'[^a-zA-Z0-9_.-]', '_', sanitized)

# === LLM / attachment handling ===
def generate_code_from_brief(request_data: BuildRequest, existing_code: str = None) -> Tuple[Dict[str, str], Dict[str, bytes]]:
    if not config.AIPIPE_TOKEN:
        raise HTTPException(status_code=503, detail="Server configuration error: AIPIPE_TOKEN is not set.")

    client = OpenAI(base_url="https://aipipe.org/openrouter/v1", api_key=config.AIPIPE_TOKEN)
    attachments_content = ""
    binary_files_to_commit: Dict[str, bytes] = {}

    # === Attachment handling ===
    if request_data.attachments:
        for attachment in request_data.attachments:
            try:
                # --- NEW: Supports both Base64 data URLs and direct HTTP URLs ---
                if attachment.url.startswith("data:"):
                    # Handle Base64 encoded data
                    header, encoded = attachment.url.split(",", 1)
                    mime_type = header.split(";")[0].split(":")[1]
                    decoded_bytes = base64.b64decode(encoded)
                elif attachment.url.startswith("http"):
                    # Handle downloadable files
                    print(f"Downloading attachment from URL: {attachment.url}")
                    response = requests.get(attachment.url, timeout=30)
                    response.raise_for_status()
                    decoded_bytes = response.content
                    mime_type = response.headers.get("Content-Type", "application/octet-stream")
                else:
                    print(f"Unsupported attachment URL format: {attachment.url}")
                    continue

                # Size limit guard
                if len(decoded_bytes) > config.MAX_ATTACHMENT_SIZE:
                    print(f"Attachment {attachment.name} too large. Skipping.")
                    continue

                # Handle image attachments
                if mime_type.startswith("image/"):
                    safe_filename = sanitize_filename(attachment.name)
                    binary_files_to_commit[safe_filename] = decoded_bytes

                    attachments_content += f"\n\n--- Attachment: `{safe_filename}` (Image file) ---\n"
                    attachments_content += f"Original URL: {attachment.url}\n"
                    attachments_content += (
                        "IMPORTANT: Use this URL as the default/fallback image in your generated code.\n"
                    )

                # Handle text-like attachments
                elif mime_type.startswith("text/") or mime_type in (
                    "application/json",
                    "application/javascript",
                ):
                    try:
                        text_content = decoded_bytes.decode('utf-8', errors='ignore')
                        attachments_content += (
                            f"\n\n--- Attachment: `{attachment.name}` ---\n```\n"
                            f"{text_content}\n```"
                        )
                        # CRITICAL: Save text attachments as real files
                        safe_filename = sanitize_filename(attachment.name)
                        binary_files_to_commit[safe_filename] = decoded_bytes
                    except Exception:
                        attachments_content += (
                            f"\n\n--- Attachment: `{attachment.name}` (text decode failed) ---"
                        )

                # Handle other binary files
                else:
                    safe_filename = sanitize_filename(attachment.name)
                    binary_files_to_commit[safe_filename] = decoded_bytes
                    attachments_content += (
                        f"\n\n--- Attachment: `{safe_filename}` (Binary file saved to repo) ---"
                    )

            except Exception as e:
                print(f"Warning: Could not process attachment '{attachment.name}'. Error: {e}")
                traceback.print_exc()

    # === Build technical requirement text ===
    technical_requirements = ""
    if request_data.checks:
        technical_requirements += "\n\n**Technical Implementation Requirements (CRITICAL):**\n"
        for i, check in enumerate(request_data.checks):
            technical_requirements += f"{i+1}. The generated webpage MUST pass this JavaScript evaluation: `{check}`\n"

    # === Define generation action ===
    action = (
        "modify an existing HTML file"
        if existing_code
        else "create a new, self-contained `index.html` file"
    )
    existing_code_section = (
        f"**EXISTING CODE:**\n```html\n{existing_code}\n```" if existing_code else ""
    )

    # === Final prompt template ===
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
- **For text attachments (CSV, JSON, Markdown)**: Fetch from relative path (e.g., fetch("data.csv")) unless ?url= parameter is provided
- If ?url= parameter is present, fetch from that URL; otherwise fetch from the local file in the repo root

**CRITICAL INSTRUCTIONS FOR FILE GENERATION:**
- Generate ALL files requested in the brief, including .txt, .json, .md, .svg, and .html.
- The 'index.html' file should link to all other files you generate.
- DO NOT generate a 'README.md' or 'LICENSE' file. They will be added automatically.

{tech_reqs}
"""

    final_prompt = prompt_template.format(
        action=action,
        existing_code_section=existing_code_section,
        brief=request_data.brief,
        attachments=attachments_content,
        tech_reqs=technical_requirements,
    )

    # === LLM call ===
    try:
        completion = client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[{"role": "user", "content": final_prompt}],
            temperature=0.1,
            timeout=120.0,
            # IMPORTANT: Request JSON output
            response_format={"type": "json_object"},
        )
        generated_content = completion.choices[0].message.content.strip()

        # Remove markdown wrappers if any
        if generated_content.startswith("```json"):
            generated_content = generated_content.split("```json", 1)[1].rsplit("```", 1)[0]
        
        # Parse the JSON string into a Python dictionary
        import json
        generated_files_dict = json.loads(generated_content)

        # Return the dictionary of files AND the binary attachments
        return generated_files_dict, binary_files_to_commit

    except Exception as e:
        print(f"LLM API call or JSON parsing failed: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=504, detail=f"LLM API call or JSON parsing failed: {e}")


# === GitHub helpers ===
def get_existing_file(repo, file_path):
    try:
        return repo.get_contents(file_path, ref=repo.default_branch)
    except UnknownObjectException:
        return None

def enable_github_pages(repo):
    """
    Try to enable GitHub Pages programmatically.
    Prefer PyGithub method when available. Fallback to REST API.
    """
    owner = repo.owner.login
    repo_name = repo.name
    branch = repo.default_branch
    print(f"Attempting to enable GitHub Pages for {owner}/{repo_name} on branch {branch} (root).")

    # Try PyGithub high-level API first
    try:
        create_pages = getattr(repo, "create_pages_site", None)
        if callable(create_pages):
            try:
                repo.create_pages_site(source_type="branch", source_branch=branch, source_path="/")
                print("GitHub Pages enabled via PyGithub.")
                return True
            except GithubException as e:
                print(f"PyGithub create_pages_site failed: {e.status} {e.data if hasattr(e,'data') else e}")
        else:
            print("PyGithub create_pages_site not available in this PyGithub version.")
    except Exception as e:
        print(f"PyGithub pages attempt raised: {e}")

    # Fallback: use GitHub REST API
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
        print(f"REST API Pages enable returned {response.status_code}: {response.text}")
    except requests.RequestException as e:
        print(f"REST API Pages enable attempt failed: {e}")

    print("Could not enable GitHub Pages programmatically. Manual enabling may be required.")
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

def create_or_update_file(repo, file_path: str, commit_message: str, content: str):
    """
    Create or update a text file. content must be a str.
    """
    file = get_existing_file(repo, file_path)
    if file:
        existing = file.decoded_content.decode("utf-8")
        if existing != content:
            repo.update_file(file.path, commit_message, content, file.sha, branch=repo.default_branch)
            print(f"Updated file: {file_path}")
        else:
            print(f"No change for file: {file_path}")
    else:
        repo.create_file(file_path, commit_message, content, branch=repo.default_branch)
        print(f"Created file: {file_path}")

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

    # README and LICENSE content
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
    license_text = """
MIT License

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

    for filename, content in generated_files.items():
        if not content: # Skip empty files
            print(f"Skipping empty file: {filename}")
            continue
        create_or_update_file(repo, filename, f"Create/Update {filename}", str(content))
    create_or_update_file(repo, "README.md", "Create/Update README", readme_content)
    create_or_update_file(repo, "LICENSE", "Create/Update LICENSE", license_text)

    # Save attachments: text as text, binary as base64
    for filename, content_bytes in binary_files.items():
        safe_name = sanitize_filename(filename)
        # Detect text files by extension
        if safe_name.endswith(('.csv', '.txt', '.md', '.json', '.js', '.xml', '.yaml', '.yml')):
            # Save as plain text
            text_content = content_bytes.decode('utf-8', errors='ignore')
            create_or_update_file(repo, safe_name, f"Add/Update attachment {safe_name}", text_content)
        else:
            # Save binary as base64 with .b64 suffix
            b64_text = base64.b64encode(content_bytes).decode("utf-8")
            b64_path = f"{safe_name}.b64"
            create_or_update_file(repo, b64_path, f"Add/Update binary attachment {safe_name}.b64", b64_text)

    # Attempt to enable GitHub Pages
    pages_enabled = enable_github_pages(repo)
    if not pages_enabled:
        print("Warning: GitHub Pages may not be enabled. Check repository settings manually.")

    commit_sha = repo.get_branch(repo.default_branch).commit.sha
    pages_url = f"https://{config.GITHUB_USERNAME}.github.io/{repo_name}/"

    # Wait and verify
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

    # === THIS IS THE CORRECTED LOOP ===
    # It now iterates over the 'generated_files' dictionary
    for filename, content in generated_files.items():
        if not content:
            print(f"Skipping empty file: {filename}")
            continue
        create_or_update_file(repo, filename, f"Create/Update {filename} (Round {request_data.round})", str(content))
    
    # Update README
    create_or_update_file(repo, "README.md", f"Update README for Round {request_data.round}", new_readme_content)

    # === This part for binary_files is correct ===
    for filename, content in binary_files.items():
        safe_name = sanitize_filename(filename)
        # Detect text files by extension
        if safe_name.endswith(('.csv', '.txt', '.md', '.json', '.js', '.xml', '.yaml', '.yml')):
            text_content = content.decode('utf-8', errors='ignore')
            create_or_update_file(repo, safe_name, f"Add/Update attachment {safe_name}", text_content)
        else:
            b64_text = base64.b64encode(content).decode("utf-8")
            b64_path = f"{safe_name}.b64"
            create_or_update_file(repo, b64_path, f"Add/Update binary attachment {safe_name}.b64", b64_text)

    # Ensure pages enabled
    enable_github_pages(repo)

    commit_sha = repo.get_branch(repo.default_branch).commit.sha
    pages_url = f"https://{config.GITHUB_USERNAME}.github.io/{repo.name}/"

    verify_deployment(pages_url, config.DEPLOYMENT_TIMEOUT)
    return repo.html_url, commit_sha, pages_url

# === Notification ===
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
            if i < 3: time.sleep(delay)
    print("All notification attempts failed.")
    return False

# === Background task ===
def run_build_and_deploy_task(request_data: BuildRequest):
    print(f"Starting background task for '{request_data.task}', round {request_data.round}.")
    try:
        if request_data.round == 1:
            # === MODIFIED LINE ===
            generated_files, binary_files = generate_code_from_brief(request_data)
            repo_url, commit_sha, pages_url = create_and_deploy(request_data, generated_files, binary_files)
        else:
            g = Github(config.GITHUB_TOKEN)
            user = g.get_user()
            repo = g.get_repo(f"{user.login}/{request_data.task}")

            file_content = get_existing_file(repo, "index.html") # This will only get index.html, see note below
            if not file_content: raise ValueError("Could not retrieve existing code for revision.")

            existing_code = file_content.decoded_content.decode("utf-8")


            generated_files, binary_files = generate_code_from_brief(request_data, existing_code)
            repo_url, commit_sha, pages_url = revise_and_deploy(request_data, generated_files, binary_files)

        notification_payload = {
            "email": request_data.email, "task": request_data.task,
            "round": request_data.round, "nonce": request_data.nonce,
            "repo_url": repo_url, "commit_sha": commit_sha,
            "pages_url": pages_url, "evaluation_url": str(request_data.evaluation_url)
        }

        notified = notify_evaluation_server(notification_payload)
        if not notified:
            print(f"CRITICAL (background): Build for '{request_data.task}' succeeded, but FAILED to notify server.")

        print(f"Background task for '{request_data.task}' completed successfully!")

    except Exception as e:
        print(f"FATAL ERROR in background task for '{request_data.task}': {e}")
        traceback.print_exc()

# === API endpoint ===
@app.post("/api/build")
def handle_build_request(request_data: BuildRequest, background_tasks: BackgroundTasks):
    if request_data.secret != config.MY_SECRET:
        raise HTTPException(status_code=403, detail="Authentication failed: Invalid secret.")

    if not all([config.AIPIPE_TOKEN, config.GITHUB_TOKEN, config.GITHUB_USERNAME]):
        raise HTTPException(status_code=503, detail="Server is not fully configured. Missing environment variables.")

    request_data.task = sanitize_filename(request_data.task)

    # Use background task to run the heavy process
    background_tasks.add_task(run_build_and_deploy_task, request_data)

    return {"status": "accepted", "message": "The build and deploy process has been started in the background."}


@app.get("/")
async def root():
    return {"message": "Task Receiver Service is running. Post to /api/build to submit a task."}
