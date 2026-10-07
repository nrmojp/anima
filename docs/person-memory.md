# Person memory identification and consolidation

Person memory is identified by a person ID within a sandbox. Do not merge people
based only on their display names. The dashboard shows the document's first
heading (name or preferred form of address), memory item count, and person ID.
If there is no heading, it shows a person-ID label. Document content is not
interpreted as HTML.

A full-document update during sleep does not mean preserving every existing
item. Retain preferred names, traits, preferences, relationships, and important
experiences. Consolidate repeated artwork sharing or praise into existing items
rather than adding an entry every day. Organize transient events, but do not
remove important information merely to reduce the item count.
Keep facts about the person separate from third-party introductions, and
preserve dates, context, and provenance after consolidation.

The default safety limits of 80 lines and 10 strong memories, and the storage
format, remain unchanged. No batch operation directly rewrites existing files.
Starting with the next sleep cycle, the sleep v7 prompt consolidates memory and
passes it to the normal synchronization flow. Selecting lines at the safety
limit is not a substitute for semantic consolidation.

Unit tests verify display metadata, fallback behavior, and prompt contracts.
The model's consolidation quality requires separate live validation; a reduction
in item count is not guaranteed.
