ALTER TABLE job_results ADD COLUMN IF NOT EXISTS demo_document_id varchar(36);
ALTER TABLE job_results ADD CONSTRAINT ck_job_results_corpus_target CHECK (document_id IS NULL OR demo_document_id IS NULL);
ALTER TABLE job_results ADD CONSTRAINT uq_job_results_demo_revision UNIQUE (id, demo_document_id);

CREATE TABLE IF NOT EXISTS demo_documents (
	document_id VARCHAR(36) NOT NULL,
	demo_source_id VARCHAR(128) NOT NULL,
	title TEXT NOT NULL,
	category_id VARCHAR(128) NOT NULL,
	catalog_metadata JSON NOT NULL,
	user_id TEXT NOT NULL,
	namespace VARCHAR(255) NOT NULL,
	status VARCHAR(32) NOT NULL,
	current_job_result_id VARCHAR(36),
	source_file_name TEXT,
	document_metadata JSON,
	parse_track VARCHAR(32) NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	archived_at TIMESTAMP WITHOUT TIME ZONE,
	PRIMARY KEY (document_id),
	CONSTRAINT ck_demo_documents_namespace CHECK (namespace = '__knowhere_demo__'),
	UNIQUE (demo_source_id)
)

;
CREATE INDEX IF NOT EXISTS idx_demo_documents_current_job_result ON demo_documents (current_job_result_id);
CREATE INDEX IF NOT EXISTS idx_demo_documents_user_namespace_status ON demo_documents (user_id, namespace, status);

CREATE TABLE IF NOT EXISTS demo_retrieval_namespace_generations (
	id VARCHAR(100) NOT NULL,
	user_id TEXT NOT NULL,
	namespace VARCHAR(255) NOT NULL,
	generation BIGINT NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_demo_retrieval_namespace_generations_scope UNIQUE (user_id, namespace)
)

;

CREATE TABLE IF NOT EXISTS demo_retrieval_namespace_map_snapshots (
	id VARCHAR(100) NOT NULL,
	user_id TEXT NOT NULL,
	namespace VARCHAR(255) NOT NULL,
	generation BIGINT NOT NULL,
	format_version INTEGER NOT NULL,
	payload_zlib BYTEA NOT NULL,
	checksum VARCHAR(64) NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_demo_retrieval_namespace_map_snapshots_scope UNIQUE (user_id, namespace)
)

;

CREATE TABLE IF NOT EXISTS demo_retrieval_hit_stats (
	id VARCHAR(36) NOT NULL,
	user_id TEXT NOT NULL,
	namespace VARCHAR(255) NOT NULL,
	hit_kind VARCHAR(32) NOT NULL,
	document_id VARCHAR(36) NOT NULL,
	chunk_id VARCHAR(64),
	hit_count INTEGER NOT NULL,
	last_hit_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(document_id) REFERENCES demo_documents (document_id) ON DELETE CASCADE
)

;
CREATE INDEX IF NOT EXISTS idx_demo_retrieval_hit_stats_chunk ON demo_retrieval_hit_stats (chunk_id);
CREATE INDEX IF NOT EXISTS idx_demo_retrieval_hit_stats_document ON demo_retrieval_hit_stats (document_id);
CREATE INDEX IF NOT EXISTS idx_demo_retrieval_hit_stats_scope_kind ON demo_retrieval_hit_stats (user_id, namespace, hit_kind);
CREATE UNIQUE INDEX IF NOT EXISTS uq_demo_retrieval_hit_stats_chunk_key ON demo_retrieval_hit_stats (user_id, namespace, hit_kind, document_id, chunk_id) WHERE chunk_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_demo_retrieval_hit_stats_document_key ON demo_retrieval_hit_stats (user_id, namespace, hit_kind, document_id) WHERE chunk_id IS NULL;

CREATE TABLE IF NOT EXISTS demo_document_map_unit_indexes (
	id VARCHAR(100) NOT NULL,
	document_id VARCHAR(36) NOT NULL,
	job_result_id VARCHAR(36) NOT NULL,
	format_version INTEGER NOT NULL,
	unit_count INTEGER NOT NULL,
	token_count INTEGER NOT NULL,
	average_idf_path FLOAT NOT NULL,
	average_idf_content FLOAT NOT NULL,
	path_document_count INTEGER,
	path_total_length INTEGER,
	content_document_count INTEGER,
	content_total_length INTEGER,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT fk_demo_documentmapunitindex_revision FOREIGN KEY(job_result_id, document_id) REFERENCES job_results (id, demo_document_id) ON DELETE RESTRICT,
	CONSTRAINT uq_demo_document_map_unit_indexes_revision UNIQUE (document_id, job_result_id),
	FOREIGN KEY(document_id) REFERENCES demo_documents (document_id) ON DELETE CASCADE,
	FOREIGN KEY(job_result_id) REFERENCES job_results (id) ON DELETE RESTRICT
)

;
CREATE INDEX IF NOT EXISTS idx_demo_document_map_unit_indexes_revision ON demo_document_map_unit_indexes (document_id, job_result_id);

CREATE TABLE IF NOT EXISTS demo_document_sections (
	section_id VARCHAR(36) NOT NULL,
	user_id TEXT NOT NULL,
	namespace VARCHAR(255) NOT NULL,
	document_id VARCHAR(36) NOT NULL,
	job_result_id VARCHAR(36) NOT NULL,
	parent_section_id VARCHAR(36),
	section_path TEXT NOT NULL,
	section_title TEXT,
	section_level INTEGER NOT NULL,
	summary TEXT,
	section_metadata JSON,
	sort_order INTEGER NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (section_id),
	CONSTRAINT uq_demo_sections_identity UNIQUE (section_id, document_id, job_result_id),
	CONSTRAINT fk_demo_sections_parent_revision FOREIGN KEY(parent_section_id, document_id, job_result_id) REFERENCES demo_document_sections (section_id, document_id, job_result_id) DEFERRABLE INITIALLY DEFERRED,
	CONSTRAINT fk_demo_documentsection_revision FOREIGN KEY(job_result_id, document_id) REFERENCES job_results (id, demo_document_id) ON DELETE RESTRICT,
	CONSTRAINT uq_demo_document_sections_revision_path UNIQUE (document_id, job_result_id, section_path),
	FOREIGN KEY(document_id) REFERENCES demo_documents (document_id) ON DELETE CASCADE,
	FOREIGN KEY(job_result_id) REFERENCES job_results (id) ON DELETE RESTRICT,
	FOREIGN KEY(parent_section_id) REFERENCES demo_document_sections (section_id) ON DELETE SET NULL
)

;
CREATE INDEX IF NOT EXISTS idx_demo_document_sections_doc_revision ON demo_document_sections (document_id, job_result_id);
CREATE INDEX IF NOT EXISTS idx_demo_document_sections_revision_snapshot_order ON demo_document_sections (document_id, job_result_id, sort_order, section_id);
CREATE INDEX IF NOT EXISTS idx_demo_document_sections_scope ON demo_document_sections (user_id, namespace);

CREATE TABLE IF NOT EXISTS demo_graph_nodes (
	node_id VARCHAR(128) NOT NULL,
	user_id TEXT NOT NULL,
	namespace VARCHAR(255) NOT NULL,
	node_kind VARCHAR(32) NOT NULL,
	owner_document_id VARCHAR(36) NOT NULL,
	job_result_id VARCHAR(36) NOT NULL,
	ref_document_id VARCHAR(36),
	ref_section_id VARCHAR(36),
	properties JSON,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (node_id),
	CONSTRAINT fk_demo_graphnode_revision FOREIGN KEY(job_result_id, owner_document_id) REFERENCES job_results (id, demo_document_id) ON DELETE RESTRICT,
	FOREIGN KEY(owner_document_id) REFERENCES demo_documents (document_id) ON DELETE CASCADE,
	FOREIGN KEY(job_result_id) REFERENCES job_results (id) ON DELETE RESTRICT
)

;
CREATE INDEX IF NOT EXISTS idx_demo_graph_nodes_owner_revision ON demo_graph_nodes (owner_document_id, job_result_id);
CREATE INDEX IF NOT EXISTS idx_demo_graph_nodes_ref_document ON demo_graph_nodes (ref_document_id);
CREATE INDEX IF NOT EXISTS idx_demo_graph_nodes_ref_section ON demo_graph_nodes (ref_section_id);
CREATE INDEX IF NOT EXISTS idx_demo_graph_nodes_scope ON demo_graph_nodes (user_id, namespace, node_kind);
CREATE INDEX IF NOT EXISTS idx_demo_graph_nodes_top_entities_gin ON demo_graph_nodes USING gin ((properties::jsonb -> 'top_entities'));
CREATE INDEX IF NOT EXISTS idx_demo_graph_nodes_top_keywords_gin ON demo_graph_nodes USING gin ((properties::jsonb -> 'top_keywords'));

CREATE TABLE IF NOT EXISTS demo_retrieval_serving_revision_manifests (
	id VARCHAR(100) NOT NULL,
	user_id TEXT NOT NULL,
	namespace VARCHAR(255) NOT NULL,
	document_id VARCHAR(36) NOT NULL,
	job_result_id VARCHAR(36) NOT NULL,
	format_version INTEGER NOT NULL,
	payload_zlib BYTEA NOT NULL,
	checksum VARCHAR(64) NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT fk_demo_retrievalservingrevisionmanifest_revision FOREIGN KEY(job_result_id, document_id) REFERENCES job_results (id, demo_document_id) ON DELETE RESTRICT,
	CONSTRAINT uq_demo_retrieval_serving_revision_manifests_revision UNIQUE (document_id, job_result_id),
	FOREIGN KEY(document_id) REFERENCES demo_documents (document_id) ON DELETE CASCADE,
	FOREIGN KEY(job_result_id) REFERENCES job_results (id) ON DELETE RESTRICT
)

;
CREATE INDEX IF NOT EXISTS idx_demo_retrieval_serving_revision_manifests_scope ON demo_retrieval_serving_revision_manifests (user_id, namespace, document_id, job_result_id);

CREATE TABLE IF NOT EXISTS demo_document_chunks (
	id VARCHAR(36) NOT NULL,
	chunk_id VARCHAR(64) NOT NULL,
	user_id TEXT NOT NULL,
	namespace VARCHAR(255) NOT NULL,
	document_id VARCHAR(36) NOT NULL,
	job_result_id VARCHAR(36) NOT NULL,
	section_id VARCHAR(36),
	chunk_type VARCHAR(64) NOT NULL,
	content TEXT,
	content_lexical_text TEXT,
	path_lexical_text TEXT,
	content_search_text TEXT,
	path_search_text TEXT,
	term_search_text TEXT,
	source_chunk_path TEXT,
	file_path TEXT,
	chunk_metadata JSON,
	sort_order INTEGER NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT fk_demo_documentchunk_section_revision FOREIGN KEY(section_id, document_id, job_result_id) REFERENCES demo_document_sections (section_id, document_id, job_result_id) DEFERRABLE INITIALLY DEFERRED,
	CONSTRAINT fk_demo_documentchunk_revision FOREIGN KEY(job_result_id, document_id) REFERENCES job_results (id, demo_document_id) ON DELETE RESTRICT,
	CONSTRAINT uq_demo_document_chunks_revision_path UNIQUE (document_id, job_result_id, source_chunk_path),
	FOREIGN KEY(document_id) REFERENCES demo_documents (document_id) ON DELETE CASCADE,
	FOREIGN KEY(job_result_id) REFERENCES job_results (id) ON DELETE RESTRICT,
	FOREIGN KEY(section_id) REFERENCES demo_document_sections (section_id) ON DELETE SET NULL
)

;
CREATE INDEX IF NOT EXISTS idx_demo_document_chunks_chunk_id ON demo_document_chunks (chunk_id);
CREATE INDEX IF NOT EXISTS idx_demo_document_chunks_doc_revision ON demo_document_chunks (document_id, job_result_id);
CREATE INDEX IF NOT EXISTS idx_demo_document_chunks_revision_section_order ON demo_document_chunks (document_id, job_result_id, section_id, sort_order, chunk_id, id);
CREATE INDEX IF NOT EXISTS idx_demo_document_chunks_revision_snapshot_order ON demo_document_chunks (document_id, job_result_id, sort_order, chunk_id, id);
CREATE INDEX IF NOT EXISTS idx_demo_document_chunks_scope ON demo_document_chunks (user_id, namespace);
CREATE INDEX IF NOT EXISTS idx_demo_document_chunks_section ON demo_document_chunks (section_id);

CREATE TABLE IF NOT EXISTS demo_document_map_units (
	id VARCHAR(160) NOT NULL,
	document_id VARCHAR(36) NOT NULL,
	job_result_id VARCHAR(36) NOT NULL,
	unit_id VARCHAR(128) NOT NULL,
	section_id VARCHAR(36) NOT NULL,
	unit_kind VARCHAR(32) NOT NULL,
	path_token_count INTEGER NOT NULL,
	content_token_count INTEGER NOT NULL,
	has_image BOOLEAN NOT NULL,
	has_table BOOLEAN NOT NULL,
	sort_order INTEGER NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT fk_demo_documentmapunit_section_revision FOREIGN KEY(section_id, document_id, job_result_id) REFERENCES demo_document_sections (section_id, document_id, job_result_id) DEFERRABLE INITIALLY DEFERRED,
	CONSTRAINT fk_demo_documentmapunit_revision FOREIGN KEY(job_result_id, document_id) REFERENCES job_results (id, demo_document_id) ON DELETE RESTRICT,
	FOREIGN KEY(document_id) REFERENCES demo_documents (document_id) ON DELETE CASCADE,
	FOREIGN KEY(job_result_id) REFERENCES job_results (id) ON DELETE RESTRICT
)

;
CREATE INDEX IF NOT EXISTS idx_demo_document_map_units_has_image ON demo_document_map_units (document_id, job_result_id) WHERE has_image IS true;
CREATE INDEX IF NOT EXISTS idx_demo_document_map_units_has_table ON demo_document_map_units (document_id, job_result_id) WHERE has_table IS true;
CREATE INDEX IF NOT EXISTS idx_demo_document_map_units_revision_order ON demo_document_map_units (document_id, job_result_id, sort_order, unit_id);
CREATE INDEX IF NOT EXISTS idx_demo_document_map_units_section ON demo_document_map_units (section_id);

CREATE TABLE IF NOT EXISTS demo_graph_edges (
	edge_id VARCHAR(160) NOT NULL,
	user_id TEXT NOT NULL,
	namespace VARCHAR(255) NOT NULL,
	edge_kind VARCHAR(32) NOT NULL,
	source_node_id VARCHAR(128) NOT NULL,
	target_node_id VARCHAR(128) NOT NULL,
	owner_document_id VARCHAR(36) NOT NULL,
	job_result_id VARCHAR(36) NOT NULL,
	is_directed BOOLEAN NOT NULL,
	weight FLOAT,
	properties JSON,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (edge_id),
	CONSTRAINT fk_demo_graphedge_revision FOREIGN KEY(job_result_id, owner_document_id) REFERENCES job_results (id, demo_document_id) ON DELETE RESTRICT,
	FOREIGN KEY(source_node_id) REFERENCES demo_graph_nodes (node_id) ON DELETE CASCADE,
	FOREIGN KEY(target_node_id) REFERENCES demo_graph_nodes (node_id) ON DELETE CASCADE,
	FOREIGN KEY(owner_document_id) REFERENCES demo_documents (document_id) ON DELETE CASCADE,
	FOREIGN KEY(job_result_id) REFERENCES job_results (id) ON DELETE RESTRICT
)

;
CREATE INDEX IF NOT EXISTS idx_demo_graph_edges_owner_revision ON demo_graph_edges (owner_document_id, job_result_id);
CREATE INDEX IF NOT EXISTS idx_demo_graph_edges_scope ON demo_graph_edges (user_id, namespace, edge_kind);
CREATE INDEX IF NOT EXISTS idx_demo_graph_edges_source ON demo_graph_edges (source_node_id);
CREATE INDEX IF NOT EXISTS idx_demo_graph_edges_target ON demo_graph_edges (target_node_id);

CREATE TABLE IF NOT EXISTS demo_document_map_unit_tokens (
	id VARCHAR(36) NOT NULL,
	map_unit_id VARCHAR(160) NOT NULL,
	channel VARCHAR(16) NOT NULL,
	token TEXT NOT NULL,
	token_hash VARCHAR(64) NOT NULL,
	frequency INTEGER NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(map_unit_id) REFERENCES demo_document_map_units (id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED
)

;
CREATE INDEX IF NOT EXISTS idx_demo_document_map_unit_tokens_token_lookup_binary ON demo_document_map_unit_tokens (channel, decode(token_hash, 'hex'::text)) INCLUDE (map_unit_id, token, frequency);
CREATE INDEX IF NOT EXISTS idx_demo_document_map_unit_tokens_unit ON demo_document_map_unit_tokens (map_unit_id, channel) INCLUDE (token, frequency);
ALTER TABLE demo_documents ADD CONSTRAINT fk_demo_documents_current_result FOREIGN KEY (current_job_result_id) REFERENCES job_results(id) ON DELETE RESTRICT;
ALTER TABLE job_results ADD CONSTRAINT fk_job_results_demo_document FOREIGN KEY (demo_document_id) REFERENCES demo_documents(document_id) ON DELETE RESTRICT;
CREATE INDEX IF NOT EXISTS ix_job_results_demo_document_id ON job_results (demo_document_id);
CREATE INDEX IF NOT EXISTS idx_demo_document_chunks_term_trgm ON demo_document_chunks USING gin (lower(COALESCE(term_search_text, '')) gin_trgm_ops);
ALTER TABLE demo_documents ADD CONSTRAINT fk_demo_documents_current_revision FOREIGN KEY (current_job_result_id, document_id) REFERENCES job_results(id, demo_document_id) DEFERRABLE INITIALLY DEFERRED;
