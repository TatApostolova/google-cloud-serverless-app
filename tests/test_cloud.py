import datetime
import os
import sys
import time
from google.cloud import storage
from google.cloud import bigquery
from google.auth import default

def run_integration_test():
    print("==========================================================")
    print("Starting Cloud End-to-End Integration Test")
    print("==========================================================")

    # 1. Detect GCP Project
    try:
        credentials, project_id = default()
        if not project_id:
            print("ERROR: Could not detect active Google Cloud Project ID.")
            print("Please ensure your gcloud CLI is configured correctly: gcloud auth application-default login")
            sys.exit(1)
        print(f"Detected Project ID: {project_id}")
    except Exception as e:
        print(f"Authentication Error: {e}")
        print("Please authenticate using: gcloud auth application-default login")
        sys.exit(1)

    # Configuration matching deploy defaults
    bucket_name = os.environ.get("BUCKET_NAME", f"document-ingestion-{project_id}")
    dataset_name = os.environ.get("BIGQUERY_DATASET", "document_processing")
    table_name = os.environ.get("BIGQUERY_TABLE", "processed_documents")
    table_ref = f"{project_id}.{dataset_name}.{table_name}"

    # Generate unique test filename
    timestamp = int(time.time())
    test_filename = f"integration-tests/test_invoice_{timestamp}.txt"
    test_content = "This is a test invoice document for urgent client billing. Contains final report data."

    print(f"GCS Bucket: {bucket_name}")
    print(f"BigQuery Table: {table_ref}")
    print(f"Uploading file: {test_filename}")

    # 2. Upload file to GCS
    try:
        storage_client = storage.Client(project=project_id)
        bucket = storage_client.bucket(bucket_name)
        
        if not bucket.exists():
            print(f"ERROR: Bucket {bucket_name} does not exist. Did you run deploy.sh/deploy.ps1?")
            sys.exit(1)
            
        blob = bucket.blob(test_filename)
        blob.upload_from_string(test_content, content_type="text/plain")
        print("File uploaded successfully to GCS!")
    except Exception as e:
        print(f"ERROR: Failed to upload file to GCS: {e}")
        sys.exit(1)

    # 3. Poll BigQuery for the record
    print("\nWaiting for pipeline trigger (GCS -> Pub/Sub -> Cloud Run -> BigQuery)...")
    bq_client = bigquery.Client(project=project_id)
    
    # Query to look for our unique file
    query = f"""
        SELECT filename, bucket, size, content_type, word_count, tags, ocr_text_preview, process_timestamp
        FROM `{table_ref}`
        WHERE filename = @filename
        LIMIT 1
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("filename", "STRING", test_filename)
        ]
    )

    max_attempts = 12
    sleep_interval = 5
    success = False

    for attempt in range(1, max_attempts + 1):
        print(f"Checking BigQuery (Attempt {attempt}/{max_attempts})...")
        try:
            query_job = bq_client.query(query, job_config=job_config)
            results = list(query_job.result())
            
            if len(results) > 0:
                row = results[0]
                print("\n==========================================================")
                print("SUCCESS: Found processed document record in BigQuery!")
                print("==========================================================")
                print(f"Filename         : {row.filename}")
                print(f"Bucket           : {row.bucket}")
                print(f"Size (Bytes)     : {row.size}")
                print(f"Content Type     : {row.content_type}")
                print(f"Word Count       : {row.word_count}")
                print(f"Tags             : {row.tags}")
                print(f"OCR Preview      : {row.ocr_text_preview}")
                print(f"Processed At     : {row.process_timestamp}")
                print("==========================================================")
                success = True
                break
        except Exception as e:
            print(f"Warning: BigQuery query failed: {e}")
            
        time.sleep(sleep_interval)

    if not success:
        print("\n==========================================================")
        print("FAILURE: Timed out waiting for BigQuery record.")
        print("Please check the Cloud Run service logs or Pub/Sub subscription metrics.")
        print("==========================================================")
        sys.exit(1)

if __name__ == "__main__":
    run_integration_test()
