import base64
import datetime
import json
import logging
import os
import sys

from flask import Flask, request, jsonify
from google.cloud import storage
from google.cloud import bigquery
from google import genai
from google.genai import types

# Configure logging to stdout
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Environment variables with defaults
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT") # BQ Client will auto-detect if None
DATASET_ID = os.environ.get("BIGQUERY_DATASET", "document_processing")
TABLE_ID = os.environ.get("BIGQUERY_TABLE", "processed_documents")
VERTEX_AI_LOCATION = os.environ.get("VERTEX_AI_LOCATION", os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"))
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# Initialize GCP clients lazily
storage_client = None
bigquery_client = None
genai_client = None

def get_storage_client():
    global storage_client
    if storage_client is None:
        storage_client = storage.Client()
    return storage_client

def get_bigquery_client():
    global bigquery_client
    if bigquery_client is None:
        bigquery_client = bigquery.Client()
    return bigquery_client

def get_genai_client():
    global genai_client
    if genai_client is None:
        genai_client = genai.Client(
            vertexai=True,
            project=PROJECT_ID,
            location=VERTEX_AI_LOCATION,
        )
    return genai_client

def extract_tags_and_count(text):
    """
    Counts words in text and searches for specific keywords to generate tags.
    """
    words = text.split()
    word_count = len(words)
    
    # Simple keyword-based tag extraction
    keywords = {
        "invoice": "invoice",
        "receipt": "receipt",
        "billing": "invoice",
        "report": "report",
        "summary": "report",
        "urgent": "urgent",
        "priority": "urgent",
        "confidential": "confidential",
        "secret": "confidential",
        "draft": "draft",
        "final": "final"
    }
    
    tags = set()
    text_lower = text.lower()
    for kw, tag in keywords.items():
        if kw in text_lower:
            tags.add(tag)
            
    # Default tags if none found
    if not tags:
        tags.add("general")
        
    return word_count, list(tags)

def extract_text_with_gemini(bucket_name, file_name, content_type):
    """
    Uses Gemini Flash on Vertex AI to extract readable text from a GCS object.
    """
    gcs_uri = f"gs://{bucket_name}/{file_name}"
    mime_type = content_type or "application/octet-stream"
    logger.info(f"Extracting text with Gemini Flash from {gcs_uri}")

    try:
        response = get_genai_client().models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                types.Part.from_uri(file_uri=gcs_uri, mime_type=mime_type),
                (
                    "Extract all readable text from this file. "
                    "Return only the extracted text, with no summary or commentary."
                ),
            ],
        )
    except Exception as e:
        logger.error(f"Gemini text extraction failed for {gcs_uri}: {e}")
        raise

    extracted_text = (getattr(response, "text", None) or "").strip()
    if not extracted_text:
        raise ValueError(f"Gemini returned no extracted text for {gcs_uri}")

    return extracted_text

@app.route("/", methods=["POST"])
@app.route("/process", methods=["POST"])
def process_document():
    """
    Receives Pub/Sub push messages, downloads the file from GCS,
    performs Gemini OCR / metadata extraction, and streams to BigQuery.
    """
    envelope = request.get_json()
    if not envelope:
        logger.error("No JSON payload received.")
        return "Bad Request: Missing JSON envelope", 400

    if "message" not in envelope:
        logger.error("Invalid Pub/Sub envelope structure.")
        return "Bad Request: Missing message field", 400

    pubsub_message = envelope["message"]
    
    # Check if there is data in the message
    if "data" not in pubsub_message:
        logger.warning("Pub/Sub message contains no data field. Skipping.")
        return "OK", 200

    try:
        # 1. Decode GCS event data
        data_str = base64.b64decode(pubsub_message["data"]).decode("utf-8")
        gcs_event = json.loads(data_str)
        
        # Log event for tracking
        logger.info(f"Received GCS Event: {json.dumps(gcs_event)}")
        
        # Verify event type (GCS Pub/Sub notifications might publish metageneration/delete events too)
        # We only care about file creation/finalization
        # GCS pubsub notifications use "OBJECT_FINALIZE" in attributes (eventTime, eventType)
        # Let's read attributes to see if it is a deletion event
        attributes = pubsub_message.get("attributes", {})
        event_type = attributes.get("eventType")
        
        if event_type and event_type != "OBJECT_FINALIZE":
            logger.info(f"Skipping non-finalize event: {event_type}")
            return "OK", 200

        bucket_name = gcs_event.get("bucket")
        file_name = gcs_event.get("name")
        size = int(gcs_event.get("size", 0))
        content_type = gcs_event.get("contentType", "application/octet-stream")

        if not bucket_name or not file_name:
            logger.error("GCS Event payload missing bucket or name.")
            return "Bad Request: Missing bucket or name", 400

        logger.info(f"Processing file: gs://{bucket_name}/{file_name} (Size: {size} bytes, ContentType: {content_type})")

        # 2. Gemini OCR and metadata extraction logic
        word_count = 0
        tags = []
        ocr_text_preview = ""

        try:
            extracted_text = extract_text_with_gemini(bucket_name, file_name, content_type)
            word_count, tags = extract_tags_and_count(extracted_text)
            ocr_text_preview = extracted_text[:500]
            logger.info(f"Successfully extracted text with Gemini. Word count: {word_count}, Tags: {tags}")
        except Exception as e:
            logger.error(f"Failed to extract text from file: {e}")
            # Fail-fast: return 500 so Pub/Sub retries delivery.
            raise

        # 3. Stream Metadata to BigQuery
        bq_client = get_bigquery_client()
        
        # Format table reference
        table_ref = f"{bq_client.project}.{DATASET_ID}.{TABLE_ID}"
        
        # Prepare row
        process_timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        row = {
            "filename": file_name,
            "bucket": bucket_name,
            "size": size,
            "content_type": content_type,
            "word_count": word_count,
            "tags": ",".join(tags),
            "ocr_text_preview": ocr_text_preview,
            "process_timestamp": process_timestamp
        }
        
        logger.info(f"Streaming metadata row to BigQuery {table_ref}: {json.dumps(row)}")
        
        # Insert rows using BQ Legacy Streaming API
        errors = bq_client.insert_rows_json(table_ref, [row])
        
        if errors:
            logger.error(f"Failed to insert row into BigQuery: {errors}")
            raise Exception(f"BigQuery insert error: {errors}")

        logger.info("Successfully processed document and updated BigQuery.")
        return "Success", 200

    except Exception as e:
        logger.error(f"Error processing message: {str(e)}", exc_info=True)
        # Fail-Fast: return HTTP 500 so Pub/Sub retries delivery
        return f"Internal Server Error: {str(e)}", 500

if __name__ == "__main__":
    # Local development server
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=True)
