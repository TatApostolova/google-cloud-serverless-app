import streamlit as st
from google.cloud import bigquery
import pandas as pd

# Page config for high-end feel
st.set_page_config(
    page_title="Document Pipeline Monitor",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Premium Style Tweaks
st.markdown("""
    <style>
    .main {
        background-color: #fcfcfc;
    }
    .metric-card {
        border-radius: 10px;
        padding: 15px;
        background-color: #ffffff;
        box-shadow: 0 4px 6px rgba(0,0,0,0.05);
        border: 1px solid #f0f0f0;
    }
    </style>
""", unsafe_allow_html=True)

# App Title & Description
st.title("⚡ Document Pipeline Monitor")
st.markdown("Real-time view of processed files, extracted tags, word counts, and OCR summaries.")

# Initialize GCP Clients
@st.cache_resource
def get_bq_client():
    return bigquery.Client()

try:
    bq_client = get_bq_client()
except Exception as e:
    st.error(f"Failed to initialize BigQuery client: {e}")
    st.info("Ensure you have run `gcloud auth application-default login` on your system.")
    st.stop()

# Fetch data from BigQuery table
@st.cache_data(ttl=10) # Refresh cache every 10 seconds
def fetch_document_records():
    table_ref = "dayagents.document_processing.processed_documents"
    query = f"""
        SELECT 
            filename, 
            process_timestamp, 
            tags, 
            word_count, 
            content_type, 
            ocr_text_preview
        FROM `{table_ref}`
        ORDER BY process_timestamp DESC
    """
    query_job = bq_client.query(query)
    # Convert to pandas DataFrame
    return query_job.to_dataframe()

# Load Data
try:
    with st.spinner("Loading records from BigQuery..."):
        df = fetch_document_records()
except Exception as e:
    st.error(f"Error executing BigQuery query: {e}")
    st.info("Check if your GCP credentials have read permissions on the table `dayagents.document_processing.processed_documents`.")
    st.stop()

# Sidebar Filters
st.sidebar.header("Filter Documents")
tag_filter = st.sidebar.text_input("Filter by Tag:", placeholder="e.g. invoice, urgent")

# Tag Filtering Logic
if tag_filter:
    tag_clean = tag_filter.strip().lower()
    def filter_func(val):
        if isinstance(val, (list, pd.Series, tuple)):
            return any(tag_clean in str(t).lower() for t in val)
        elif isinstance(val, str):
            return tag_clean in val.lower()
        return False
    
    filtered_df = df[df['tags'].apply(filter_func)]
else:
    filtered_df = df

# Metrics dashboard at the top
col1, col2, col3 = st.columns(3)
with col1:
    st.metric("Total Processed", len(filtered_df))
with col2:
    total_words = int(filtered_df['word_count'].sum()) if not filtered_df.empty else 0
    st.metric("Total Words Extracted", f"{total_words:,}")
with col3:
    avg_words = filtered_df['word_count'].mean() if not filtered_df.empty else 0
    st.metric("Avg Words per Doc", f"{avg_words:.1f}")

st.write("---")

# Main dataframe
st.subheader("Extracted Document Metadata")
if filtered_df.empty:
    st.info("No documents found matching the filter criteria.")
else:
    # Render dataframe with friendly column headers
    st.dataframe(
        filtered_df,
        column_config={
            "filename": "Filename",
            "process_timestamp": st.column_config.DatetimeColumn(
                "Processed At",
                format="YYYY-MM-DD HH:mm:ss"
            ),
            "tags": "Tags",
            "word_count": "Word Count",
            "content_type": "Content Type",
            "ocr_text_preview": "OCR Preview"
        },
        use_container_width=True,
        hide_index=True
    )

    st.write("---")

    # Document inspection details
    st.subheader("🔍 OCR Text Preview Inspector")
    selected_filename = st.selectbox(
        "Choose a document to inspect full OCR text:", 
        options=filtered_df["filename"].unique()
    )
    
    selected_row = filtered_df[filtered_df["filename"] == selected_filename].iloc[0]
    
    col_a, col_b = st.columns([1, 2])
    with col_a:
        st.markdown(f"**Filename:** `{selected_row['filename']}`")
        st.markdown(f"**Content Type:** `{selected_row['content_type']}`")
        st.markdown(f"**Word Count:** `{selected_row['word_count']}`")
        st.markdown(f"**Processed At:** `{selected_row['process_timestamp']}`")
        
        # Format tags beautifully
        tags_val = selected_row['tags']
        if isinstance(tags_val, (list, pd.Series, tuple)):
            tags_str = ", ".join(tags_val)
        else:
            tags_str = str(tags_val)
        st.markdown(f"**Tags:** `{tags_str}`")
        
    with col_b:
        st.markdown("**OCR Preview Content:**")
        st.code(selected_row["ocr_text_preview"], language="text")
