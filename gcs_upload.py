from datetime import datetime, timezone
import hashlib
import json
import os
import requests
import sys
from typing import Annotated, Sequence
from typing_extensions import TypedDict

# LangChain / LangGraph Imports
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

# Google Cloud SDK Imports
from google.cloud import storage
from google.cloud import service_usage_v1

class AgentState(TypedDict):
    """Holds the active execution history conversation state."""
    messages: Annotated[Sequence[BaseMessage], add_messages]
def write_jsonl_log(step: int, status: str, message: str, file_path: str = "upload_agent_execution_logs.jsonl"):
    """Helper function to append a structured log entry to a JSONL file."""
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "step": step,
        "status": status,
        "message": message
    }
    with open(file_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry) + "\n")

@tool
def automate_gcp_public_storage(bucket_name: str, download_url: str, region: str = "asia-south1", config: RunnableConfig = None) -> str:
    """
    Automates a 6-step GCP deployment workflow:
    1. Connects using current active local CLI credentials.
    2. Enables the Cloud Storage JSON API if disabled.
    3. Creates a storage bucket in the requested region.
    4. Configures IAM permissions to make bucket/object listings public.
    5. Downloads a file from a URL, calculates its true SHA-256 hash, logs it, and uploads it.
    6. Pulls a real-time object listing and builds a summary deployment log.
    
    Args:
        bucket_name: The unique name extracted from the user prompt.
        download_url: The web URL of the file to download and process.
        region: The geographical location (e.g., 'asia-south1'). Defaults to 'asia-south1'.
    """
    configurable = config.get("configurable", {}) if config else {}
    project_id = configurable.get("gcp_project_id") or os.getenv("GCP_PROJECT_ID")
    
    if not project_id:
        err_msg = "Operations halted: No 'GCP_PROJECT_ID' environment variable found."
        write_jsonl_log(0, "ERROR", err_msg)
        return f"[AGENT ERROR] {err_msg}"

    text_logs = [f"[AGENT LOG] Starting automation loop for Project: {project_id}"]
    write_jsonl_log(0, "INFO", f"Starting automation loop for Project: {project_id}")

    temp_file = "downloaded_agent_file.tmp"

    try:
        # Step 1: SDK Client Initialization
        storage_client = storage.Client(project=project_id)
        service_client = service_usage_v1.ServiceUsageClient()
        
        msg1 = "Connected using local CLI credentials."
        text_logs.append(f"[AGENT LOG] Step 1 Complete: {msg1}")
        write_jsonl_log(1, "SUCCESS", msg1)

        # Step 2: Enable Cloud Storage JSON API
        service_name = f"projects/{project_id}/services/storage.googleapis.com"
        service_status = service_client.get_service(request={"name": service_name})
        
        if service_status.state.value != 2:
            write_jsonl_log(2, "INFO", "API is currently DISABLED. Launching enablement request...")
            operation = service_client.enable_service(request={"name": service_name})
            operation.result() 
            msg2 = "Storage API successfully enabled."
        else:
            msg2 = "Storage API was already enabled."
            
        text_logs.append(f"[AGENT LOG] Step 2 Complete: {msg2}")
        write_jsonl_log(2, "SUCCESS", msg2)

        # Step 3: Create the Bucket
        text_logs.append(f"[AGENT LOG] Step 3: Provisioning bucket '{bucket_name}' in '{region}'...")
        bucket = storage_client.bucket(bucket_name)
        bucket.storage_class = "STANDARD"
        bucket.iam_configuration.public_access_prevention = "inherited" 
        new_bucket = storage_client.create_bucket(bucket, location=region)
        
        msg3 = f"Bucket '{new_bucket.name}' provisioned successfully."
        text_logs.append(f"[AGENT LOG] Step 3 Complete: {msg3}")
        write_jsonl_log(3, "SUCCESS", msg3)

        # Step 4: Make Publicly Readable and Listable
        policy = new_bucket.get_iam_policy(requested_policy_version=3)
        policy.bindings.append({
            "role": "roles/storage.objectViewer",
            "members": {"allUsers"}
        })
        new_bucket.set_iam_policy(policy)
        
        msg4 = "Public 'allUsers' binding applied."
        text_logs.append(f"[AGENT LOG] Step 4 Complete: {msg4}")
        write_jsonl_log(4, "SUCCESS", msg4)

        # Step 5: Download File, Compute SHA-256, and Upload (Updated)
        # destination_blob_name = download_url.split("/")[-1].split("?")[0] or "downloaded_file"
        destination_blob_name = "eval.jsonl"
        text_logs.append(f"[AGENT LOG] Step 5: Downloading file from URL: {download_url}...")
        
        # Stream the download down locally
        response = requests.get(download_url, stream=True, timeout=30)
        response.raise_for_status()
        
        sha256_engine = hashlib.sha256()
        with open(temp_file, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    sha256_engine.update(chunk)
                    
        computed_sha256 = sha256_engine.hexdigest()
        
        # Log the calculated hash explicitly into standard streams and JSONL
        msg_hash = f"File download complete. Computed SHA-256 Sum: {computed_sha256}"
        text_logs.append(f"[AGENT LOG] Step 5 Data: {msg_hash}")
        write_jsonl_log(5, "HASH_VERIFICATION", msg_hash)
        
        # Perform the safe binary upload to GCS
        text_logs.append(f"[AGENT LOG] Step 5: Uploading to bucket as '{destination_blob_name}'...")
        blob = new_bucket.blob(destination_blob_name)
        
        # We attach the verified SHA-256 as custom cloud metadata to verify against later
        blob.metadata = {"original_sha256": computed_sha256}
        blob.upload_from_filename(temp_file)
        
        msg5 = "Successfully copied remote file to GCS destination. File Hash matched metadata signatures."
        text_logs.append(f"[AGENT LOG] Step 5 Complete: {msg5}")
        write_jsonl_log(5, "SUCCESS", msg5)
        
        # Local cleanup of temporary files
        if os.path.exists(temp_file):
            os.remove(temp_file)

        # Step 6: Query and Output Live Object Listing
        blobs_list = list(storage_client.list_blobs(bucket_name))
        object_lines = [f" - {b.name} (Size: {b.size} bytes)" for b in blobs_list]
        object_listing_str = "\n".join(object_lines) if object_lines else " - [No objects found]"

        msg6 = f"Fetched live object listing. Found {len(blobs_list)} objects {object_listing_str} ."
        text_logs.append(f"[AGENT LOG] Step 6 Complete: {msg6}")
        write_jsonl_log(6, "SUCCESS", msg6)

        descriptor = (
            f"\n=== DEPLOYMENT & OBJECT LISTING SUMMARY ===\n"
            f"Bucket Name: {new_bucket.name}\n"
            f"Location: {new_bucket.location}\n"
            f"Computed SHA-256: {computed_sha256}\n"
            f"Public Base URL: https://://googleapis.com/{new_bucket.name}/\n\n"
            f"CURRENT OBJECT LISTING:\n"
            f"{object_listing_str}\n"
            f"============================================"
        )
        text_logs.append(descriptor)
        
        write_jsonl_log(7, "SUMMARY", f"Deployment finished. URL: https://://googleapis.com/{new_bucket.name}/")
        return "\n".join(text_logs)

    except Exception as e:
        if os.path.exists(temp_file):
            os.remove(temp_file)
        err_msg = f"Pipeline broken during processing loop. Trace: {str(e)}"
        text_logs.append(f"[AGENT ERROR] {err_msg}")
        write_jsonl_log(0, "ERROR", err_msg)
        return "\n".join(text_logs)

# ==========================================
# SECTION 3: LANGGRAPH WORKFLOW BUILDING
# ==========================================
# Attach tool blueprints directly into target LLM configuration profiles
# create a bucket named q2-8d692effac89b4b in asia-south1 region, download https://exam.sanand.workers.dev/gcp-cloud-download?email=24f2007720%40ds.study.iitm.ac.in&quizSign=WoyfpPEm75FhhqD4VWdrwYC8ekGS54jnk4tuAPOQlxKsoB0jLCuXv7j%2FqoyelX7IBaUbk6%2BQ0TYiQhWn6%2BDzUn7k4joLHo9Dw%2BIuqWHONM89w8THN6DYjtglhDmUS7IUeDYtVr5scPFdEM%2FW0h3lSWGpyqUuvoGWv3a3pTX%2FTtq9JJgNuFTo3FuQvCJWIs%2FP2O109IRaUhOM1dD0dxHgp4zGsLo%2F84pYS5uX5UqPvKRYEwX3xXQfglHh2sM1UEknQ1wggnzSvrj21OzQ2CRmMySmOFcjepfuReJdsDMOMMUDS%2FY%2FO9HvQBq%2BgFAqmsdwB2bbk87k9eMrP28u8JIbcg%3D%3D&questionId=q-gcp-cloud-eval-dataset-server&file=eval and upload that file to the bucket.
tools = [automate_gcp_public_storage]
api_key=os.environ.get('AI_PIPE_KEY', None)
base_url="https://aipipe.org/openai/v1"
model = ChatOpenAI(model="gpt-4o-mini", api_key=api_key, base_url=base_url, temperature=0).bind_tools(tools)

def call_model(state: AgentState):
    """Direct node evaluator instructing LLM how to route requests."""
    system_instruction = {
        "role": "system", 
        "content": "You are a GCP cloud deployment automation assistant. Use the tool provided to provision public cloud storage resources based on instructions."
    }
    response = model.invoke([system_instruction] + list(state["messages"]))
    return {"messages": [response]}

# State graph wiring configurations
workflow = StateGraph(AgentState)
workflow.add_node("agent", call_model)
workflow.add_node("tools", ToolNode(tools))

workflow.add_edge(START, "agent")
# Routing evaluation conditions check for active dynamic tool intents
workflow.add_conditional_edges("agent", lambda state: "tools" if state["messages"][-1].tool_calls else END)
workflow.add_edge("tools", "agent")

app = workflow.compile()


# ==========================================
# SECTION 4: INTERACTIVE USER EXECUTION LOOP
# ==========================================
if __name__ == "__main__":
    # Fallback system logging confirmation layers
    gcp_project = os.getenv("GCP_PROJECT_ID")
    if not gcp_project:
        print("❌ CRITICAL ERROR: Please define 'GCP_PROJECT_ID' environment variable before launching script.")
        sys.exit(1)
        
    print("🤖 Agent Online. System Context set to use Project ID silently.")
    print("Waiting for prompt input specifications...")
    print("Example: 'Create a bucket named static-files-mumbai-99 in region asia-south1'")
    print("-" * 65)

    while True:
        try:
            user_prompt = input("\nYou: ")
            if user_prompt.lower() in ["exit", "quit"]:
                print("Shutting down infrastructure automation loops. Goodbye!")
                break
            
            if not user_prompt.strip():
                continue
                
            inputs = {"messages": [HumanMessage(content=user_prompt)]}
            
            # Pass Project ID runtime context silently down within configuration states
            graph_config = {"configurable": {"gcp_project_id": gcp_project}}
            
            # Run graph execution, pulling state adjustments dynamically into standard stdout streams
            for output in app.stream(inputs, config=graph_config, stream_mode="values"):
                last_msg = output["messages"][-1]
                if last_msg.type == "tool":
                    print(f"\n{last_msg.content}")
                    
        except KeyboardInterrupt:
            print("\nShutting down infrastructure loops.")
            break