# deploy.ps1
# Google Cloud Serverless Document Processing Pipeline Deployment Script for PowerShell
#

$ErrorActionPreference = "Stop"

# Configuration
$Region = if ($env:REGION) { $env:REGION } else { "us-central1" }
$TopicName = if ($env:TOPIC_NAME) { $env:TOPIC_NAME } else { "document-upload-topic" }
$SubscriptionName = if ($env:SUBSCRIPTION_NAME) { $env:SUBSCRIPTION_NAME } else { "document-processor-subscription" }
$DatasetName = if ($env:BIGQUERY_DATASET) { $env:BIGQUERY_DATASET } else { "document_processing" }
$TableName = if ($env:BIGQUERY_TABLE) { $env:BIGQUERY_TABLE } else { "processed_documents" }
$ServiceName = if ($env:SERVICE_NAME) { $env:SERVICE_NAME } else { "document-processor" }
$RepoName = if ($env:REPO_NAME) { $env:REPO_NAME } else { "document-processor-repo" }

# Get current project ID
$ProjectId = gcloud config get-value project 2>$null

if ([string]::IsNullOrEmpty($ProjectId)) {
    Write-Error "ERROR: No active Google Cloud Project configured in gcloud CLI. Please set one using: gcloud config set project [PROJECT_ID]"
    exit 1
}

$BucketName = if ($env:BUCKET_NAME) { $env:BUCKET_NAME } else { "document-ingestion-$ProjectId" }

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "Deploying Serverless Document Processing Pipeline (PowerShell)" -ForegroundColor Cyan
Write-Host "Project ID        : $ProjectId"
Write-Host "Region            : $Region"
Write-Host "GCS Ingestion     : gs://$BucketName"
Write-Host "Pub/Sub Topic     : $TopicName"
Write-Host "Pub/Sub Sub       : $SubscriptionName"
Write-Host "BigQuery Dataset  : $DatasetName"
Write-Host "BigQuery Table    : $TableName"
Write-Host "Cloud Run Service : $ServiceName"
Write-Host "==========================================================" -ForegroundColor Cyan

Write-Host "Step 1: Enabling Required GCP Service APIs..." -ForegroundColor Green
gcloud services enable `
    storage.googleapis.com `
    pubsub.googleapis.com `
    artifactregistry.googleapis.com `
    run.googleapis.com `
    bigquery.googleapis.com `
    cloudbuild.googleapis.com

Write-Host "Step 2: Creating Cloud Storage Ingestion Bucket..." -ForegroundColor Green
$bucketExists = $true
try {
    gcloud storage buckets describe "gs://$BucketName" --format="value(name)" 2>$null | Out-Null
} catch {
    $bucketExists = $false
}

if ($bucketExists) {
    Write-Host "GCS Bucket gs://$BucketName already exists."
} else {
    gcloud storage buckets create "gs://$BucketName" --project="$ProjectId" --location="$Region"
    Write-Host "GCS Bucket gs://$BucketName created successfully."
}

Write-Host "Step 3: Creating BigQuery Dataset & Table..." -ForegroundColor Green
# Dataset
$datasetExists = $true
try {
    bq show --project_id="$ProjectId" "$DatasetName" 2>$null | Out-Null
} catch {
    $datasetExists = $false
}

if ($datasetExists) {
    Write-Host "BigQuery Dataset $DatasetName already exists."
} else {
    bq --project_id="$ProjectId" mk --dataset --location="$Region" "$DatasetName"
    Write-Host "BigQuery Dataset $DatasetName created successfully."
}

# Table
$tableRef = "$DatasetName.$TableName"
$tableExists = $true
try {
    bq show --project_id="$ProjectId" "$tableRef" 2>$null | Out-Null
} catch {
    $tableExists = $false
}

if ($tableExists) {
    Write-Host "BigQuery Table $tableRef already exists."
} else {
    $schema = "filename:STRING:REQUIRED,bucket:STRING:REQUIRED,size:INTEGER:REQUIRED,content_type:STRING,word_count:INTEGER,tags:STRING:REPEATED,ocr_text_preview:STRING,process_timestamp:TIMESTAMP:REQUIRED"
    bq --project_id="$ProjectId" mk --table "$tableRef" "$schema"
    Write-Host "BigQuery Table $tableRef created successfully."
}

Write-Host "Step 4: Creating Pub/Sub Topic..." -ForegroundColor Green
$topicExists = $true
try {
    gcloud pubsub topics describe "$TopicName" --project="$ProjectId" --format="value(name)" 2>$null | Out-Null
} catch {
    $topicExists = $false
}

if ($topicExists) {
    Write-Host "Pub/Sub Topic $TopicName already exists."
} else {
    gcloud pubsub topics create "$TopicName" --project="$ProjectId"
    Write-Host "Pub/Sub Topic $TopicName created successfully."
}

Write-Host "Step 5: Granting Pub/Sub Publisher permissions to Cloud Storage service account..." -ForegroundColor Green
$gcsServiceAccount = gcloud storage service-agent --project="$ProjectId"
gcloud pubsub topics add-iam-policy-binding "$TopicName" `
    --project="$ProjectId" `
    --member="serviceAccount:$gcsServiceAccount" `
    --role="roles/pubsub.publisher" | Out-Null

Write-Host "Step 6: Creating GCS Notification Trigger..." -ForegroundColor Green
$notificationExists = $false
try {
    $notifications = gcloud storage buckets notifications list "gs://$BucketName" --format="value(topic)" 2>$null
    if ($notifications -match $TopicName) {
        $notificationExists = $true
    }
} catch {}

if ($notificationExists) {
    Write-Host "Notification trigger already exists for gs://$BucketName -> $TopicName"
} else {
    gcloud storage notifications create "gs://$BucketName" --topic="$TopicName" --event-types="OBJECT_FINALIZE"
    Write-Host "GCS Notification trigger created successfully."
}

Write-Host "Step 7: Granting IAM Roles to Default Compute Service Account..." -ForegroundColor Green
$projectNumber = gcloud projects describe "$ProjectId" --format="value(projectNumber)"
$computeServiceAccount = "${projectNumber}-compute@developer.gserviceaccount.com"

Write-Host "Binding roles/storage.objectViewer..."
gcloud projects add-iam-policy-binding "$ProjectId" --member="serviceAccount:$computeServiceAccount" --role="roles/storage.objectViewer" | Out-Null

Write-Host "Binding roles/bigquery.dataEditor..."
gcloud projects add-iam-policy-binding "$ProjectId" --member="serviceAccount:$computeServiceAccount" --role="roles/bigquery.dataEditor" | Out-Null

Write-Host "Binding roles/bigquery.user..."
gcloud projects add-iam-policy-binding "$ProjectId" --member="serviceAccount:$computeServiceAccount" --role="roles/bigquery.user" | Out-Null

Write-Host "Step 8: Creating Docker Repository & Deploying Cloud Run Service..." -ForegroundColor Green
# Repository
$repoExists = $true
try {
    gcloud artifacts repositories describe "$RepoName" --project="$ProjectId" --location="$Region" --format="value(name)" 2>$null | Out-Null
} catch {
    $repoExists = $false
}

if ($repoExists) {
    Write-Host "Artifact Registry repository $RepoName already exists."
} else {
    gcloud artifacts repositories create "$RepoName" --project="$ProjectId" --repository-format=docker --location="$Region" --description="Docker repository for document processor"
    Write-Host "Artifact Registry repository $RepoName created successfully."
}

$imageUrl = "${Region}-docker.pkg.dev/${ProjectId}/${RepoName}/${ServiceName}:latest"

Write-Host "Building Docker container image using Cloud Build..."
gcloud builds submit src/ --tag "$imageUrl" --project="$ProjectId"

Write-Host "Deploying container to Cloud Run..."
gcloud run deploy "$ServiceName" `
    --image "$imageUrl" `
    --region "$Region" `
    --project="$ProjectId" `
    --platform managed `
    --allow-unauthenticated `
    --update-env-vars "BIGQUERY_DATASET=${DatasetName},BIGQUERY_TABLE=${TableName}"

$serviceUrl = gcloud run services describe "$ServiceName" --region="$Region" --project="$ProjectId" --format="value(status.url)"
Write-Host "Cloud Run Service successfully deployed to: $serviceUrl" -ForegroundColor Yellow

Write-Host "Step 9: Setting up Pub/Sub Push Subscription..." -ForegroundColor Green
$subExists = $true
try {
    gcloud pubsub subscriptions describe "$SubscriptionName" --project="$ProjectId" --format="value(name)" 2>$null | Out-Null
} catch {
    $subExists = $false
}

if ($subExists) {
    Write-Host "Pub/Sub Subscription $SubscriptionName already exists. Updating endpoint..."
    gcloud pubsub subscriptions update "$SubscriptionName" --project="$ProjectId" --push-endpoint="${serviceUrl}/process"
} else {
    gcloud pubsub subscriptions create "$SubscriptionName" `
        --project="$ProjectId" `
        --topic="$TopicName" `
        --push-endpoint="${serviceUrl}/process" `
        --ack-deadline=60
    Write-Host "Pub/Sub Subscription $SubscriptionName created successfully."
}

Write-Host "==========================================================" -ForegroundColor Green
Write-Host "Deployment Complete!" -ForegroundColor Green
Write-Host "You can upload files to GCS bucket: gs://$BucketName" -ForegroundColor Yellow
Write-Host "Check BigQuery for results in table: $ProjectId.$DatasetName.$TableName" -ForegroundColor Yellow
Write-Host "==========================================================" -ForegroundColor Green
