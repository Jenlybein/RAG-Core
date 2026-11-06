# Pipeline Notes

Upload accepts the document, the worker parses it later.

## Storage Layout

The upload endpoint stores the source file and answers immediately.

| Field | Meaning |
| --- | --- |
| document_id | stable identifier |
| heading_path | section provenance |
| chunk_type | table, list or prose |

## Chunking Contract

- one heading path per section
- tables are never split across chunks
- every chunk records its section id

### Worker Steps

Claim, parse, split, embed, purge, write, commit.
