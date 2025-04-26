# PR Review Server
# Automates code analysis and documentation using Claude Desktop and Notion

import os
import uuid
import json
import hmac
import hashlib
import time
from datetime import datetime
from dotenv import load_dotenv
from flask import Flask, request, jsonify
import requests
import websocket
import threading
from github import Github
from notion_client import Client

# Load environment variables
load_dotenv()

# Environment configuration
PORT = int(os.getenv('PORT', 3000))
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')
GITHUB_WEBHOOK_SECRET = os.getenv('GITHUB_WEBHOOK_SECRET')
NOTION_TOKEN = os.getenv('NOTION_TOKEN')
NOTION_DATABASE_ID = os.getenv('NOTION_DATABASE_ID')
CLAUDE_DESKTOP_PORT = int(os.getenv('CLAUDE_DESKTOP_PORT', 8080))

# Initialize clients
github_client = Github(GITHUB_TOKEN)
notion = Client(auth=NOTION_TOKEN)

# Initialize Flask app
app = Flask(__name__)

# Global websocket connection
claude_ws = None
claude_connected = False

# MCP server for Claude Desktop communication
def setup_mcp_client():
    global claude_ws, claude_connected
    
    # Message handler for websocket
    def on_message(ws, message):
        try:
            data = json.loads(message)
            print(f"Received message from Claude Desktop: {data.get('type')}")
        except Exception as e:
            print(f"Error processing message from Claude Desktop: {e}")
    
    # Connection handler
    def on_open(ws):
        global claude_connected
        print("Connected to Claude Desktop")
        claude_connected = True
    
    # Error handler
    def on_error(ws, error):
        print(f"WebSocket error: {error}")
    
    # Close handler
    def on_close(ws, close_status_code, close_msg):
        global claude_connected
        print("Disconnected from Claude Desktop")
        claude_connected = False
        # Try to reconnect after delay
        time.sleep(5)
        connect_to_claude()
    
    # Connect to Claude Desktop
    def connect_to_claude():
        global claude_ws
        try:
            ws_url = f"ws://localhost:{CLAUDE_DESKTOP_PORT}/mcp"
            claude_ws = websocket.WebSocketApp(
                ws_url,
                on_message=on_message,
                on_open=on_open,
                on_error=on_error,
                on_close=on_close
            )
            wst = threading.Thread(target=claude_ws.run_forever)
            wst.daemon = True
            wst.start()
        except Exception as e:
            print(f"Error connecting to Claude Desktop: {e}")
    
    connect_to_claude()

# Verify GitHub webhook signature
def verify_github_webhook(request_data, signature_header):
    if not GITHUB_WEBHOOK_SECRET:
        print("Warning: GITHUB_WEBHOOK_SECRET not configured")
        return True
    
    if not signature_header:
        return False
    
    # Get expected signature
    expected_signature = "sha256=" + hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(),
        request_data,
        hashlib.sha256
    ).hexdigest()
    
    try:
        # Compare signatures using constant time comparison
        return hmac.compare_digest(signature_header, expected_signature)
    except Exception as e:
        print(f"Error verifying webhook signature: {e}")
        return False

# Get PR data from GitHub
def get_pr_data(owner, repo, pr_number):
    try:
        # Get repository
        repo_obj = github_client.get_repo(f"{owner}/{repo}")
        
        # Get pull request
        pull_request = repo_obj.get_pull(pr_number)
        
        # Get files
        files = pull_request.get_files()
        
        # Get commits
        commits = pull_request.get_commits()
        
        return {
            "pullRequest": pull_request,
            "files": list(files),
            "commits": list(commits)
        }
    except Exception as e:
        print(f"Error fetching PR data: {e}")
        raise e

# Get file content
def get_file_content(owner, repo, path, ref):
    try:
        repo_obj = github_client.get_repo(f"{owner}/{repo}")
        content = repo_obj.get_contents(path, ref=ref)
        
        # If it's a file, return the content
        if not isinstance(content, list):
            return content.decoded_content.decode('utf-8')
        
        return None
    except Exception as e:
        print(f"Error getting file content for {path}: {e}")
        return None

# Send PR data to Claude Desktop for analysis
def analyze_with_claude(pr_data):
    global claude_ws, claude_connected
    
    if not claude_connected:
        raise Exception("Claude Desktop not connected")
    
    # Generate unique request ID
    request_id = str(uuid.uuid4())
    
    # Prepare data for Claude
    file_changes = []
    for file in pr_data["files"]:
        file_extension = os.path.splitext(file.filename)[1][1:] if os.path.splitext(file.filename)[1] else ""
        
        file_changes.append({
            "filename": file.filename,
            "status": file.status,
            "additions": file.additions,
            "deletions": file.deletions,
            "changes": file.changes,
            "patch": file.patch or "",
            "language": file_extension
        })
    
    message = {
        "type": "analyze_pr",
        "requestId": request_id,
        "data": {
            "title": pr_data["pullRequest"].title,
            "description": pr_data["pullRequest"].body or "",
            "base": pr_data["pullRequest"].base.ref,
            "head": pr_data["pullRequest"].head.ref,
            "author": pr_data["pullRequest"].user.login,
            "files": file_changes,
            "commits": [
                {
                    "sha": commit.sha,
                    "message": commit.commit.message,
                    "author": commit.commit.author.name
                }
                for commit in pr_data["commits"]
            ]
        }
    }
    
    # Set up response receiver
    response_event = threading.Event()
    response_data = {"result": None}
    
    def response_receiver(ws, message):
        try:
            data = json.loads(message)
            if data.get("type") == "analysis_result" and data.get("requestId") == request_id:
                response_data["result"] = data.get("result")
                response_event.set()
        except Exception as e:
            print(f"Error processing Claude response: {e}")
    
    # Save original handler to restore later
    original_handler = claude_ws.on_message
    
    # Set our handler
    claude_ws.on_message = response_receiver
    
    # Send message to Claude Desktop
    claude_ws.send(json.dumps(message))
    
    # Wait for response with timeout
    response_received = response_event.wait(timeout=60)  # 60 second timeout
    
    # Restore original handler
    claude_ws.on_message = original_handler
    
    if not response_received:
        raise Exception("Timeout waiting for Claude Desktop response")
    
    return response_data["result"]

# Save analysis to Notion
def save_to_notion(pr_data, analysis):
    try:
        # Create new page in Notion database
        response = notion.pages.create(
            parent={"database_id": NOTION_DATABASE_ID},
            properties={
                "Title": {
                    "title": [
                        {
                            "text": {
                                "content": f"PR #{pr_data['pullRequest'].number}: {pr_data['pullRequest'].title}"
                            }
                        }
                    ]
                },
                "Repository": {
                    "rich_text": [
                        {
                            "text": {
                                "content": pr_data["pullRequest"].base.repo.full_name
                            }
                        }
                    ]
                },
                "Author": {
                    "rich_text": [
                        {
                            "text": {
                                "content": pr_data["pullRequest"].user.login
                            }
                        }
                    ]
                },
                "URL": {
                    "url": pr_data["pullRequest"].html_url
                },
                "Status": {
                    "select": {
                        "name": "Reviewed"
                    }
                },
                "Created At": {
                    "date": {
                        "start": pr_data["pullRequest"].created_at.isoformat()
                    }
                }
            },
            children=[
                {
                    "object": "block",
                    "type": "heading_2",
                    "heading_2": {
                        "rich_text": [
                            {
                                "text": {
                                    "content": "PR Summary"
                                }
                            }
                        ]
                    }
                },
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [
                            {
                                "text": {
                                    "content": analysis.get("summary") or "No summary provided."
                                }
                            }
                        ]
                    }
                },
                {
                    "object": "block",
                    "type": "heading_2",
                    "heading_2": {
                        "rich_text": [
                            {
                                "text": {
                                    "content": "Code Analysis"
                                }
                            }
                        ]
                    }
                },
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [
                            {
                                "text": {
                                    "content": analysis.get("codeAnalysis") or "No code analysis provided."
                                }
                            }
                        ]
                    }
                },
                {
                    "object": "block",
                    "type": "heading_2",
                    "heading_2": {
                        "rich_text": [
                            {
                                "text": {
                                    "content": "Recommendations"
                                }
                            }
                        ]
                    }
                },
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [
                            {
                                "text": {
                                    "content": analysis.get("recommendations") or "No recommendations provided."
                                }
                            }
                        ]
                    }
                }
            ]
        )
        
        print(f"Analysis saved to Notion: {response['id']}")
        return response["id"]
    except Exception as e:
        print(f"Error saving to Notion: {e}")
        raise e

# Process a PR
def process_pull_request(owner, repo, pr_number):
    try:
        print(f"Processing PR #{pr_number} from {owner}/{repo}")
        
        # Get PR data
        pr_data = get_pr_data(owner, repo, pr_number)
        
        # Analyze with Claude Desktop
        print("Sending PR data to Claude Desktop for analysis...")
        analysis = analyze_with_claude(pr_data)
        
        # Save to Notion
        print("Saving analysis to Notion...")
        notion_page_id = save_to_notion(pr_data, analysis)
        
        # Optional: Comment on the PR with a link to the Notion page
        repo_obj = github_client.get_repo(f"{owner}/{repo}")
        issue = repo_obj.get_issue(pr_number)
        issue.create_comment(
            f"PR review completed! View the detailed analysis in [Notion](https://notion.so/{notion_page_id.replace('-', '')})."
        )
        
        return {
            "success": True,
            "notion_page_id": notion_page_id
        }
    except Exception as e:
        print(f"Error processing pull request: {e}")
        return {
            "success": False,
            "error": str(e)
        }

# GitHub webhook endpoint
@app.route('/webhook', methods=['POST'])
def webhook():
    # Verify webhook signature
    signature = request.headers.get('X-Hub-Signature-256')
    if not verify_github_webhook(request.data, signature):
        print("Invalid webhook signature")
        return "Unauthorized", 401
    
    event = request.headers.get('X-GitHub-Event')
    payload = request.json
    
    # Process PR events
    if event == 'pull_request':
        action = payload.get('action')
        
        # Only process opened, reopened, or synchronized PRs
        if action in ['opened', 'reopened', 'synchronize']:
            pull_request = payload.get('pull_request')
            repository = payload.get('repository')
            
            # Acknowledge receipt
            threading.Thread(
                target=process_pull_request,
                args=(
                    repository['owner']['login'],
                    repository['name'],
                    pull_request['number']
                )
            ).start()
            
            return "Processing PR", 202
        
        return "Event ignored", 200
    
    return "Event type not handled", 200

# Health check endpoint
@app.route('/health', methods=['GET'])
def health():
    status = {
        "server": "up",
        "claude_desktop": "connected" if claude_connected else "disconnected"
    }
    return jsonify(status)

# Manual trigger endpoint for testing
@app.route('/analyze/<owner>/<repo>/<int:pr>', methods=['POST'])
def analyze(owner, repo, pr):
    if not claude_connected:
        return jsonify({"error": "Claude Desktop not connected"}), 503
    
    try:
        result = process_pull_request(owner, repo, pr)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    # Setup Claude Desktop connection
    setup_mcp_client()
    
    # Start Flask server
    app.run(host='0.0.0.0', port=PORT)
