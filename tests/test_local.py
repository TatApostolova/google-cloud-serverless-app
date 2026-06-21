import base64
import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# Add src folder to system path so we can import main
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

import main

class TestLocalProcessor(unittest.TestCase):
    def setUp(self):
        # Configure app for testing
        main.app.config['TESTING'] = True
        self.client = main.app.test_client()
        
        # Reset clients
        main.storage_client = None
        main.bigquery_client = None
        main.genai_client = None

    @patch('main.genai.Client')
    @patch('main.bigquery.Client')
    def test_process_txt_file(self, mock_bq_class, mock_genai_class):
        # Setup mocks
        mock_genai_client = MagicMock()
        mock_genai_class.return_value = mock_genai_client
        mock_genai_response = MagicMock()
        mock_genai_response.text = "This is a confidential invoice for billing report and urgent priority items."
        mock_genai_client.models.generate_content.return_value = mock_genai_response
        
        mock_bq_client = MagicMock()
        mock_bq_class.return_value = mock_bq_client
        mock_bq_client.project = "test-project-123"
        mock_bq_client.insert_rows_json.return_value = [] # No errors

        # Create Pub/Sub message envelope
        gcs_payload = {
            "bucket": "my-test-bucket",
            "name": "invoices/invoice_2026.txt",
            "size": "76",
            "contentType": "text/plain"
        }
        
        data_bytes = json.dumps(gcs_payload).encode('utf-8')
        encoded_data = base64.b64encode(data_bytes).decode('utf-8')
        
        payload = {
            "message": {
                "data": encoded_data,
                "messageId": "11223344",
                "publishTime": "2026-06-21T05:36:32Z",
                "attributes": {
                    "eventType": "OBJECT_FINALIZE"
                }
            }
        }

        # Send POST request
        response = self.client.post('/process', json=payload)
        
        # Assertions
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data.decode(), "Success")
        
        # Verify Gemini extraction was called with the GCS object.
        mock_genai_client.models.generate_content.assert_called_once()
        _, generate_kwargs = mock_genai_client.models.generate_content.call_args
        self.assertEqual(generate_kwargs["model"], main.GEMINI_MODEL)
        
        # Verify BQ insert was called
        mock_bq_client.insert_rows_json.assert_called_once()
        args, kwargs = mock_bq_client.insert_rows_json.call_args
        
        table_ref = args[0]
        rows = args[1]
        
        self.assertEqual(table_ref, "test-project-123.document_processing.processed_documents")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        
        self.assertEqual(row["filename"], "invoices/invoice_2026.txt")
        self.assertEqual(row["bucket"], "my-test-bucket")
        self.assertEqual(row["size"], 76)
        self.assertEqual(row["content_type"], "text/plain")
        self.assertEqual(row["word_count"], 12) # 12 words in the test string
        # Tags should contain invoice, confidential, report, urgent
        self.assertIn("invoice", row["tags"])
        self.assertIn("confidential", row["tags"])
        self.assertIn("report", row["tags"])
        self.assertIn("urgent", row["tags"])
        self.assertIn("This is a confidential invoice", row["ocr_text_preview"])
        self.assertTrue(row["process_timestamp"].endswith("Z") or "+00:00" in row["process_timestamp"])

    @patch('main.genai.Client')
    @patch('main.bigquery.Client')
    def test_process_binary_file_gemini_ocr(self, mock_bq_class, mock_genai_class):
        # Setup mocks
        mock_genai_client = MagicMock()
        mock_genai_class.return_value = mock_genai_client
        mock_genai_response = MagicMock()
        mock_genai_response.text = "Annual report final summary with revenue and confidential notes."
        mock_genai_client.models.generate_content.return_value = mock_genai_response
        
        mock_bq_client = MagicMock()
        mock_bq_class.return_value = mock_bq_client
        mock_bq_client.project = "test-project-123"
        mock_bq_client.insert_rows_json.return_value = [] # No errors

        # Create Pub/Sub message envelope for a PDF file
        gcs_payload = {
            "bucket": "my-test-bucket",
            "name": "annual_report.pdf",
            "size": "524288",
            "contentType": "application/pdf"
        }
        
        data_bytes = json.dumps(gcs_payload).encode('utf-8')
        encoded_data = base64.b64encode(data_bytes).decode('utf-8')
        
        payload = {
            "message": {
                "data": encoded_data,
                "messageId": "55667788",
                "publishTime": "2026-06-21T05:36:32Z",
                "attributes": {
                    "eventType": "OBJECT_FINALIZE"
                }
            }
        }

        # Send POST request
        response = self.client.post('/process', json=payload)
        
        # Assertions
        self.assertEqual(response.status_code, 200)
        
        # Verify Gemini OCR was called with the uploaded GCS object.
        mock_genai_client.models.generate_content.assert_called_once()

        # Verify BQ insert was called with Gemini-derived data.
        mock_bq_client.insert_rows_json.assert_called_once()
        args, kwargs = mock_bq_client.insert_rows_json.call_args
        rows = args[1]
        row = rows[0]
        
        self.assertEqual(row["filename"], "annual_report.pdf")
        self.assertEqual(row["content_type"], "application/pdf")
        self.assertEqual(row["size"], 524288)
        self.assertIn("report", row["tags"])
        self.assertIn("confidential", row["tags"])
        self.assertIn("Annual report final summary", row["ocr_text_preview"])

    @patch('main.storage.Client')
    @patch('main.bigquery.Client')
    def test_skips_non_finalize_events(self, mock_bq_class, mock_storage_class):
        # Create Pub/Sub message envelope for deletion event
        gcs_payload = {
            "bucket": "my-test-bucket",
            "name": "document.txt"
        }
        
        data_bytes = json.dumps(gcs_payload).encode('utf-8')
        encoded_data = base64.b64encode(data_bytes).decode('utf-8')
        
        payload = {
            "message": {
                "data": encoded_data,
                "messageId": "999999",
                "attributes": {
                    "eventType": "OBJECT_DELETE" # NOT OBJECT_FINALIZE
                }
            }
        }

        # Send POST request
        response = self.client.post('/process', json=payload)
        
        # Assertions: Should return 200 but do nothing
        self.assertEqual(response.status_code, 200)
        mock_storage_class.assert_not_called()
        mock_bq_class.assert_not_called()

    @patch('main.genai.Client')
    @patch('main.bigquery.Client')
    def test_internal_server_error_returns_500(self, mock_bq_class, mock_genai_class):
        # Setup mocks to raise exception
        mock_genai_client = MagicMock()
        mock_genai_class.return_value = mock_genai_client
        mock_genai_client.models.generate_content.side_effect = Exception("Gemini extraction error")

        # Create Pub/Sub message envelope
        gcs_payload = {
            "bucket": "my-test-bucket",
            "name": "error_file.txt",
            "size": "100",
            "contentType": "text/plain"
        }
        
        data_bytes = json.dumps(gcs_payload).encode('utf-8')
        encoded_data = base64.b64encode(data_bytes).decode('utf-8')
        
        payload = {
            "message": {
                "data": encoded_data,
                "messageId": "888888",
                "attributes": {
                    "eventType": "OBJECT_FINALIZE"
                }
            }
        }

        # Send POST request
        response = self.client.post('/process', json=payload)
        
        # Should return 500
        self.assertEqual(response.status_code, 500)
        self.assertIn("Internal Server Error", response.data.decode())

if __name__ == '__main__':
    unittest.main()
