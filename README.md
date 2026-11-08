# TDS AI: LLM-Powered Code Deployment Agent 

| | |
|---|---|
| **What it does** | Takes a natural-language brief (with optional attachments), asks an LLM to generate a static web app, creates or updates a GitHub repository, enables GitHub Pages and checks that the live URL responds |
| **Context** | IITM Tools in Data Science course project |
| **Stack** | FastAPI, Pydantic, PyGithub and the GitHub REST API, an OpenAI-compatible client (default model `openai/gpt-4o` through the course AIPipe proxy), Docker, Hugging Face Space |
| **Built with** | AI coding tools (Claude Code, Google Antigravity) |

### How it works
1. A build request arrives at the FastAPI endpoint and is validated with Pydantic.
2. Code generation runs as a FastAPI background task.
3. The generated files are committed to a new or existing GitHub repository and GitHub Pages is enabled.
4. The service polls the Pages URL until it responds or times out.
5. A revision request updates an existing repository the same way.

### Known limitations
- Generation runs as in-process background tasks (no external job queue).
- No automated tests in this repo.


An intelligent FastAPI-based service that automatically generates HTML web applications from natural language briefs using LLM and deploys them to GitHub Pages. Perfect for rapid prototyping and automated web development!

##  Features

- **AI-Powered Code Generation**: Converts natural language briefs into production-ready HTML code using GPT-4
- **Automatic GitHub Deployment**: Creates repositories and deploys to GitHub Pages automatically
- **Iterative Development**: Supports multiple rounds of revisions and improvements
- **Attachment Support**: Handles text and binary file attachments (images, data files, etc.)
- **Technical Requirements Validation**: Ensures generated code meets specific JavaScript evaluation criteria
- **Background Processing**: Asynchronous task execution for better performance

##  Table of Contents

- [Prerequisites](#prerequisites)
- [Environment Setup](#environment-setup)
- [Local Installation](#local-installation)
- [Using via Hugging Face](#using-via-hugging-face)
- [API Documentation](#api-documentation)
- [Example Usage](#example-usage)
- [Troubleshooting](#troubleshooting)

##  Prerequisites

Before running this project, ensure you have:

- Python 3.8 or higher
- GitHub account with Personal Access Token
- AIPIPE API token (for LLM access)
- Git installed on your system

##  Environment Setup

Create a `.env` file in the project root with the following variables:

# Required Variables
AIPIPE_TOKEN=your_aipipe_token_here

GITHUB_TOKEN=your_github_personal_access_token

GITHUB_USERNAME=your_github_username

# Optional Variables
LLM_MODEL=openai/gpt-4o
MY_SECRET=your-custom-secret-key
DEPLOYMENT_TIMEOUT=180


### Getting Your Tokens

1. **AIPIPE_TOKEN**: Sign up at [aipipe.org](https://aipipe.org) and get your API key
2. **GITHUB_TOKEN**: Go to GitHub Settings → Developer Settings → Personal Access Tokens → Generate new token
   - Required scopes: `repo`, `workflow`, `admin:repo_hook`
3. **GITHUB_USERNAME**: Your GitHub username (e.g., `john-doe`)

##  Local Installation

### Step 1: Clone the Repository

```
git clone https://github.com/yourusername/tds-ai-deployment.git
cd tds-ai-deployment
```

### Step 2: Create Virtual Environment

```
# Create virtual environment
python -m venv venv

# Activate virtual environment
# On Windows:
venv\Scripts\activate
# On macOS/Linux:
source venv/bin/activate
```

### Step 3: Install Dependencies

```
pip install -r requirements.txt
```

### Step 4: Configure Environment Variables

Create `.env` file with your tokens (see [Environment Setup](#environment-setup))

### Step 5: Run the Application

```
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Your API will be available at: `http://localhost:8000`

Access interactive API docs at: `http://localhost:8000/docs`

##  Using via Hugging Face

The service is deployed on Hugging Face Spaces and can be accessed directly via API calls.

### Hugging Face Endpoint

```
https://24f2007692-llm-code-deployer.hf.space/api/build
```

### Making API Calls to Hugging Face

#### Using cURL

```
curl -X POST "https://24f2007692-llm-code-deployer.hf.space/api/build" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "student@example.com",
    "secret": "my-super-secret-123",
    "task": "my-awesome-project",
    "round": 1,
    "nonce": "unique-request-id-123",
    "brief": "Create a responsive landing page with a hero section, features grid, and contact form",
    "evaluation_url": "https://your-callback-url.com/evaluate"
  }'
```

#### Using Python Requests

```
import requests

url = "https://24f2007692-llm-code-deployer.hf.space/api/build"

payload = {
    "email": "student@example.com",
    "secret": "my-super-secret-123",
    "task": "my-awesome-project",
    "round": 1,
    "nonce": "unique-request-id-123",
    "brief": "Create a responsive landing page with a hero section, features grid, and contact form",
    "evaluation_url": "https://your-callback-url.com/evaluate"
}

response = requests.post(url, json=payload)
print(response.json())
```

##  API Documentation

### Endpoint: `/api/build`

**Method**: `POST`

**Content-Type**: `application/json`

### Request Body Schema

```
{
  "email": "string (required)",
  "secret": "string (required)",
  "task": "string (required) - repository name",
  "round": "integer (required) - 1 for new, >1 for revision",
  "nonce": "string (required) - unique request identifier",
  "brief": "string (required) - natural language description",
  "evaluation_url": "string (required) - callback URL",
  "attachments": [
    {
      "name": "string (optional)",
      "url": "string (optional) - base64 data URL"
    }
  ],
  "checks": [
    "string (optional) - JavaScript evaluation criteria"
  ]
}
```

### Response

**Status Code**: `202 Accepted`

```
{
  "status": "accepted",
  "message": "The build and deploy process has been started in the background."
}
```

### Callback to Evaluation URL

Once deployment is complete, the service sends a POST request to your `evaluation_url`:

```
{
  "email": "student@example.com",
  "task": "my-awesome-project",
  "round": 1,
  "nonce": "unique-request-id-123",
  "repo_url": "https://github.com/username/my-awesome-project",
  "commit_sha": "abc123def456...",
  "pages_url": "https://username.github.io/my-awesome-project/"
}
```

##  Example Usage

### Example 1: Create a Simple Landing Page

```
{
  "email": "student@example.com",
  "secret": "my-super-secret-123",
  "task": "portfolio-website",
  "round": 1,
  "nonce": "req-001",
  "brief": "Create a modern portfolio website with dark mode, smooth scrolling, and animated sections showing projects and skills",
  "evaluation_url": "https://webhook.site/your-unique-id"
}
```

### Example 2: Revision with Technical Requirements

```
{
  "email": "student@example.com",
  "secret": "my-super-secret-123",
  "task": "portfolio-website",
  "round": 2,
  "nonce": "req-002",
  "brief": "Add a contact form with email validation and update the color scheme to blue tones",
  "evaluation_url": "https://webhook.site/your-unique-id",
  "checks": [
    "document.querySelector('form') !== null",
    "document.querySelector('input[type=\"email\"]') !== null"
  ]
}
```

### Example 3: With Image Attachment

```
import base64
import requests

# Read and encode image
with open("logo.png", "rb") as img_file:
    img_data = base64.b64encode(img_file.read()).decode()
    img_url = f"data:image/png;base64,{img_data}"

payload = {
    "email": "student@example.com",
    "secret": "my-super-secret-123",
    "task": "company-website",
    "round": 1,
    "nonce": "req-003",
    "brief": "Create a company website using the provided logo",
    "evaluation_url": "https://webhook.site/your-unique-id",
    "attachments": [
        {
            "name": "logo.png",
            "url": img_url
        }
    ]
}

response = requests.post("http://localhost:8000/api/build", json=payload)
print(response.json())
```

##  Troubleshooting

### Common Issues

**Issue**: "AIPIPE_TOKEN is not set"
- **Solution**: Ensure your `.env` file contains `AIPIPE_TOKEN=your_token`

**Issue**: "GitHub Pages not deploying"
- **Solution**: Check your GitHub token has correct permissions. Manually enable GitHub Pages in repository settings if needed.

**Issue**: "Deployment verification timed out"
- **Solution**: GitHub Pages can take 5-10 minutes to deploy. Increase `DEPLOYMENT_TIMEOUT` in `.env`

**Issue**: 403 Authentication Error
- **Solution**: Verify your `secret` matches the `MY_SECRET` environment variable

### Logs

Check application logs for detailed error messages:

```
# Local deployment
# Logs appear in terminal where uvicorn is running

# Hugging Face deployment
# Check logs in Space settings → Logs tab
```

##  Project Structure

```
tds-ai-deployment/
├── main.py              # Main FastAPI application
├── .env                 # Environment variables (create this)
├── requirements.txt     # Python dependencies
├── README.md           # This file
└── .gitignore          # Git ignore rules
```

##  License

MIT License - Feel free to use this project for learning and development!

##  Contributing

Contributions are welcome! Feel free to:
- Report bugs
- Suggest features
- Submit pull requests

##  Support

For issues and questions:
- Create an issue on GitHub
- Email: 24f2007692@ds.study.iitm.ac.in

## Credits

Developed as part of the TDS (Tools in Data Science) course project at IIT Madras BS Degree in Data Science.

---

Made with ❤️ by KarTiK🍁
```
