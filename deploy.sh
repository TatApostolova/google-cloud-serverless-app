










#!/usr/bin/env bash
#
# deploy.sh
# Google Cloud Serverless Document Processing Pipeline Deployment Script
#

# Exit immediately if a command exits with a non-zero status
set -e

# Configuration
REGION="${REGION:-us-central1}"
TOPIC_NAME="${TOPIC_NAME:-document-upload-topic}"
SUBSCRIPTION_NAME="${SUBSCRIPTION_NAME:-document-processor-subscription}"
DATASET_NAME="${DATASET_NAME:-document_processing}"
TABLE_NAME="${TABLE_NAME:-processed_documents}"
SERVICE_NAME="${SERVICE_NAME:-document-processor}"
REPO_NAME="${REPO_NAME:-document-processor-repo}"

# Get current project ID
PROJECT_ID=$(gcloud config get-value project 2>/dev/null)

if [ -z "$PROJECT_ID" ]; then
    echo "ERROR: No active Google Cloud Project configured in gcloud CLI."
    echo "Please set one using: gcloud config set project [PROJECT_ID]"
    exit 1
fi

BUCKET_NAME="${BUCKET_NAME:-document-ingestion-${PROJECT_ID}}"

echo "=========================================================="
echo "Deploying Serverless Document Processing Pipeline"
echo "Project ID        : $PROJECT_ID"
echo "Region            : $REGION"
echo "GCS Ingestion     : gs://$BUCKET_NAME"
echo "Pub/Sub Topic     : $TOPIC_NAME"
echo "Pub/Sub Sub       : $SUBSCRIPTION_NAME"
echo "BigQuery Dataset  : $DATASET_NAME"
echo "BigQuery Table    : $TABLE_NAME"
echo "Cloud Run Service : $SERVICE_NAME"
echo "=========================================================="

echo "Step 1: Enabling Required GCP Service APIs..."
gcloud services enable \
    storage.googleapis.com \
    pubsub.googleapis.com \
    artifactregistry.googleapis.com \
    run.googleapis.com \
    bigquery.googleapis.com \
    cloudbuild.googleapis.com

echo "Step 2: Creating Cloud Storage Ingestion Bucket..."
if gcloud storage buckets describe "gs://${BUCKET_NAME}" &>/dev/null; then
    echo "GCS Bucket gs://${BUCKET_NAME} already exists."
else
    gcloud storage buckets create "gs://${BUCKET_NAME}" \
        --project="$PROJECT_ID" \
        --location="$REGION"
    echo "GCS Bucket gs://${BUCKET_NAME} created successfully."
fi

echo "Step 3: Creating BigQuery Dataset & Table..."
# Create Dataset if it doesn't exist
if bq show --project_id="$PROJECT_ID" "$DATASET_NAME" &>/dev/null; then
    echo "BigQuery Dataset $DATASET_NAME already exists."
else
    bq --project_id="$PROJECT_ID" mk --dataset \
        --location="$REGION" \
        "$DATASET_NAME"
    echo "BigQuery Dataset $DATASET_NAME created successfully."
fi

# Create Table if it doesn't exist
TABLE_REF="${DATASET_NAME}.${TABLE_NAME}"
if bq show --project_id="$PROJECT_ID" "$TABLE_REF" &>/dev/null; then
    echo "BigQuery Table $TABLE_REF already exists."
else
    # Schema matches design decisions (Extended Schema with tags as REPEATED string)

SCHEMA="filename:STRING,bucket:STRING,size:INTEGER,content_type:STRING,word_count:INTEGER,tags:STRING,ocr_text_preview:STRING,process_timestamp:TIMESTAMP"   
 bq --project_id="$PROJECT_ID" mk --table \
        "$TABLE_REF" \
        "$SCHEMA"
    echo "BigQuery Table $TABLE_REF created successfully."
fi

echo "Step 4: Creating Pub/Sub Topic..."
if gcloud pubsub topics describe "$TOPIC_NAME" --project="$PROJECT_ID" &>/dev/null; then
    echo "Pub/Sub Topic $TOPIC_NAME already exists."
else
    gcloud pubsub topics create "$TOPIC_NAME" --project="$PROJECT_ID"
    echo "Pub/Sub Topic $TOPIC_NAME created successfully."
fi

echo "Step 5: Granting Pub/Sub Publisher permissions to Cloud Storage service account..."
# GCS publishes notifications to Pub/Sub, so GCS Service Agent needs Publisher role on the topic.
GCS_SERVICE_ACCOUNT=$(gcloud storage service-agent --project="$PROJECT_ID" | tr -d '[:space:]')
gcloud pubsub topics add-iam-policy-binding "$TOPIC_NAME" \
    --project="$PROJECT_ID" \
    --member="serviceAccount:${GCS_SERVICE_ACCOUNT}" \
    --role="roles/pubsub.publisher"

echo "Step 6: Creating GCS Notification Trigger..."
# Create storage notification configuration linking Bucket -> Pub/Sub topic.
# Only trigger on finalized object creations.
if gsutil notification list "gs://${BUCKET_NAME}" 2>/dev/null | grep -q "$TOPIC_NAME"; then
    echo "Notification trigger already exists for gs://${BUCKET_NAME} -> $TOPIC_NAME"
else
    gsutil notification create \
        -t "$TOPIC_NAME" \
        -f json \
        -e OBJECT_FINALIZE \
        "gs://${BUCKET_NAME}"
    echo "GCS Notification trigger created successfully."
fi
# Cloud Run default Service Account is the Compute default Service Account.
# It needs Storage Object Viewer, BigQuery Data Editor, and BigQuery User.
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")
COMPUTE_SERVICE_ACCOUNT="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

echo "Binding roles/storage.objectViewer..."
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${COMPUTE_SERVICE_ACCOUNT}" \
    --role="roles/storage.objectViewer" &>/dev/null

echo "Binding roles/bigquery.dataEditor..."
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${COMPUTE_SERVICE_ACCOUNT}" \
    --role="roles/bigquery.dataEditor" &>/dev/null

echo "Binding roles/bigquery.user..."
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${COMPUTE_SERVICE_ACCOUNT}" \
    --role="roles/bigquery.user" &>/dev/null

echo "Step 8: Creating Docker Repository & Deploying Cloud Run Service..."
# Create Artifact Registry repo if it doesn't exist
if gcloud artifacts repositories describe "$REPO_NAME" --project="$PROJECT_ID" --location="$REGION" &>/dev/null; then
    echo "Artifact Registry repository $REPO_NAME already exists."
else
    gcloud artifacts repositories create "$REPO_NAME" \
        --project="$PROJECT_ID" \
        --repository-format=docker \
        --location="$REGION" \
        --description="Docker repository for document processor"
    echo "Artifact Registry repository $REPO_NAME created successfully."
fi

IMAGE_URL="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/${SERVICE_NAME}:latest"

echo "Building Docker container image using Cloud Build..."
# Build and push using Cloud Build
gcloud builds submit src/ \
    --tag "$IMAGE_URL" \
    --project="$PROJECT_ID"

echo "Deploying container to Cloud Run..."
gcloud run deploy "$SERVICE_NAME" \
    --image "$IMAGE_URL" \
    --region "$REGION" \
    --project="$PROJECT_ID" \
    --platform managed \
    --allow-unauthenticated \
    --update-env-vars "BIGQUERY_DATASET=${DATASET_NAME},BIGQUERY_TABLE=${TABLE_NAME}"

# Fetch service URL
SERVICE_URL=$(gcloud run services describe "$SERVICE_NAME" --region="$REGION" --project="$PROJECT_ID" --format="value(status.url)")
echo "Cloud Run Service successfully deployed to: $SERVICE_URL"

echo "Step 9: Setting up Pub/Sub Push Subscription..."
# Check if subscription already exists
if gcloud pubsub subscriptions describe "$SUBSCRIPTION_NAME" --project="$PROJECT_ID" &>/dev/null; then
    echo "Pub/Sub Subscription $SUBSCRIPTION_NAME already exists. Updating endpoint..."
    gcloud pubsub subscriptions update "$SUBSCRIPTION_NAME" \
        --project="$PROJECT_ID" \
        --push-endpoint="${SERVICE_URL}/process"
else
    gcloud pubsub subscriptions create "$SUBSCRIPTION_NAME" \
        --project="$PROJECT_ID" \
        --topic="$TOPIC_NAME" \
        --push-endpoint="${SERVICE_URL}/process" \
        --ack-deadline=60
    echo "Pub/Sub Subscription $SUBSCRIPTION_NAME created successfully."
fi

echo "=========================================================="
echo "Deployment Complete!"
echo "You can upload files to GCS bucket: gs://${BUCKET_NAME}"
echo "Check BigQuery for results in table: ${PROJECT_ID}.${DATASET_NAME}.${TABLE_NAME}"
echo "=========================================================="
