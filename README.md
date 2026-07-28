# argonath
Digital collections processing pipeline for UC Davis Library. Ingests archival materials into CaskFS, generates image derivatives, runs OCR, produces AI-assisted Schema.org metadata via Samwise/Qwen, and indexes for RAG search using LlamaIndex + pgvector. Orchestrated by Dagster.

For local dev (docker compose) and prod (libk8s) deployment, see
[argonath-deployment](https://github.com/ucd-library/argonath-deployment).
