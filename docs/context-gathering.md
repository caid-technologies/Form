# Conversational context gathering

New project conversations begin in `gathering_context`. During this phase, Forma accepts text and image or document references, appends an immutable `DesignBrief` version, persists the user and assistant messages, and returns targeted clarification questions.

```http
POST /projects/{project_id}/context/messages
Content-Type: application/json

{
  "conversation_id": "chat-id",
  "text": "Build a USB-C environmental monitor",
  "attachments": [
    {
      "kind": "image",
      "name": "reference.png",
      "media_type": "image/png",
      "data_url": "data:image/png;base64,...",
      "source": "clipboard"
    }
  ]
}
```

The context agent is deterministic and does not call an LLM, enqueue a worker job, or execute tools. Generation, iteration, fabrication, and OpenCAD mutation actions that identify a project in `gathering_context` fail with `tool_execution_blocked_while_gathering_context`. A later readiness/build action must transition the project before those tools can run.

Inline attachment bytes are not copied into the DesignBrief. The brief stores a stable reference plus media/source metadata; extracted document text is merged into its requirements.


## PDF references

The chat composer accepts PDF references in addition to images. PDF files are sent to the context endpoint as `document` attachments with `media_type: application/pdf`. The API extracts bounded page-labelled text and drops the raw PDF bytes before persisting the document reference or chat history.

PDF ingestion is multimodal. Forma scores each page for visual content using embedded-image and vector-drawing signals plus low-text/scanned-page detection. Up to 6 relevant pages are rendered into a compact contact sheet, stored as a derived `uploaded_image` reference, and passed through the existing generation worker's image-input path. The derived image reference records its source PDF and original page numbers so the visual context remains traceable.

Current ingestion limits are 2 MB, 40 pages, 20,000 extracted text characters, 6 rendered visual pages, and a 1.5 MB visual contact sheet. Password-protected PDFs are rejected. Scanned/image-only PDFs are accepted when their pages can be rendered, even when no text layer is available.

```json
{
  "kind": "document",
  "name": "motor-datasheet.pdf",
  "media_type": "application/pdf",
  "data_url": "data:application/pdf;base64,...",
  "source": "upload"
}
```
