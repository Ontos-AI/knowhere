CREATE OR REPLACE FUNCTION is_demo_revision_ready(document_identity text, revision_identity text)
RETURNS boolean LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
 SELECT EXISTS (
  SELECT 1 FROM demo_documents d JOIN job_results r ON r.demo_document_id = d.document_id
  JOIN demo_document_map_unit_indexes i ON i.document_id = d.document_id AND i.job_result_id = r.id
  WHERE d.document_id = document_identity AND r.id = revision_identity AND d.status = 'active'
 )
$$;
ALTER TABLE demo_documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE demo_documents FORCE ROW LEVEL SECURITY;
CREATE POLICY demo_maintainer_write ON demo_documents FOR ALL USING (COALESCE(current_setting('knowhere.demo_maintainer', true), '') <> '') WITH CHECK (COALESCE(current_setting('knowhere.demo_maintainer', true), '') <> '');
CREATE POLICY demo_public_read ON demo_documents FOR SELECT USING (status = 'active');
ALTER TABLE demo_retrieval_namespace_generations ENABLE ROW LEVEL SECURITY;
ALTER TABLE demo_retrieval_namespace_generations FORCE ROW LEVEL SECURITY;
CREATE POLICY demo_maintainer_write ON demo_retrieval_namespace_generations FOR ALL USING (COALESCE(current_setting('knowhere.demo_maintainer', true), '') <> '') WITH CHECK (COALESCE(current_setting('knowhere.demo_maintainer', true), '') <> '');
CREATE POLICY demo_public_read ON demo_retrieval_namespace_generations FOR SELECT USING (true);
ALTER TABLE demo_retrieval_namespace_map_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE demo_retrieval_namespace_map_snapshots FORCE ROW LEVEL SECURITY;
CREATE POLICY demo_maintainer_write ON demo_retrieval_namespace_map_snapshots FOR ALL USING (COALESCE(current_setting('knowhere.demo_maintainer', true), '') <> '') WITH CHECK (COALESCE(current_setting('knowhere.demo_maintainer', true), '') <> '');
CREATE POLICY demo_public_read ON demo_retrieval_namespace_map_snapshots FOR SELECT USING (true);
ALTER TABLE demo_retrieval_hit_stats ENABLE ROW LEVEL SECURITY;
ALTER TABLE demo_retrieval_hit_stats FORCE ROW LEVEL SECURITY;
CREATE POLICY demo_caller_stats ON demo_retrieval_hit_stats FOR ALL USING (user_id = NULLIF(current_setting('knowhere.demo_caller', true), '')) WITH CHECK (user_id = NULLIF(current_setting('knowhere.demo_caller', true), '') AND namespace = '__knowhere_demo__');
ALTER TABLE demo_document_map_unit_indexes ENABLE ROW LEVEL SECURITY;
ALTER TABLE demo_document_map_unit_indexes FORCE ROW LEVEL SECURITY;
CREATE POLICY demo_maintainer_write ON demo_document_map_unit_indexes FOR ALL USING (COALESCE(current_setting('knowhere.demo_maintainer', true), '') <> '') WITH CHECK (COALESCE(current_setting('knowhere.demo_maintainer', true), '') <> '');
CREATE POLICY demo_public_read ON demo_document_map_unit_indexes FOR SELECT USING (EXISTS (SELECT 1 FROM job_results r JOIN demo_documents d ON d.document_id = r.demo_document_id WHERE r.id = demo_document_map_unit_indexes.job_result_id AND d.document_id = demo_document_map_unit_indexes.document_id AND d.status = 'active'));
ALTER TABLE demo_document_sections ENABLE ROW LEVEL SECURITY;
ALTER TABLE demo_document_sections FORCE ROW LEVEL SECURITY;
CREATE POLICY demo_maintainer_write ON demo_document_sections FOR ALL USING (COALESCE(current_setting('knowhere.demo_maintainer', true), '') <> '') WITH CHECK (COALESCE(current_setting('knowhere.demo_maintainer', true), '') <> '');
CREATE POLICY demo_public_read ON demo_document_sections FOR SELECT USING (is_demo_revision_ready(document_id, job_result_id));
ALTER TABLE demo_graph_nodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE demo_graph_nodes FORCE ROW LEVEL SECURITY;
CREATE POLICY demo_maintainer_write ON demo_graph_nodes FOR ALL USING (COALESCE(current_setting('knowhere.demo_maintainer', true), '') <> '') WITH CHECK (COALESCE(current_setting('knowhere.demo_maintainer', true), '') <> '');
CREATE POLICY demo_public_read ON demo_graph_nodes FOR SELECT USING (is_demo_revision_ready(owner_document_id, job_result_id));
ALTER TABLE demo_retrieval_serving_revision_manifests ENABLE ROW LEVEL SECURITY;
ALTER TABLE demo_retrieval_serving_revision_manifests FORCE ROW LEVEL SECURITY;
CREATE POLICY demo_maintainer_write ON demo_retrieval_serving_revision_manifests FOR ALL USING (COALESCE(current_setting('knowhere.demo_maintainer', true), '') <> '') WITH CHECK (COALESCE(current_setting('knowhere.demo_maintainer', true), '') <> '');
CREATE POLICY demo_public_read ON demo_retrieval_serving_revision_manifests FOR SELECT USING (is_demo_revision_ready(document_id, job_result_id));
ALTER TABLE demo_document_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE demo_document_chunks FORCE ROW LEVEL SECURITY;
CREATE POLICY demo_maintainer_write ON demo_document_chunks FOR ALL USING (COALESCE(current_setting('knowhere.demo_maintainer', true), '') <> '') WITH CHECK (COALESCE(current_setting('knowhere.demo_maintainer', true), '') <> '');
CREATE POLICY demo_public_read ON demo_document_chunks FOR SELECT USING (is_demo_revision_ready(document_id, job_result_id));
ALTER TABLE demo_document_map_units ENABLE ROW LEVEL SECURITY;
ALTER TABLE demo_document_map_units FORCE ROW LEVEL SECURITY;
CREATE POLICY demo_maintainer_write ON demo_document_map_units FOR ALL USING (COALESCE(current_setting('knowhere.demo_maintainer', true), '') <> '') WITH CHECK (COALESCE(current_setting('knowhere.demo_maintainer', true), '') <> '');
CREATE POLICY demo_public_read ON demo_document_map_units FOR SELECT USING (is_demo_revision_ready(document_id, job_result_id));
ALTER TABLE demo_graph_edges ENABLE ROW LEVEL SECURITY;
ALTER TABLE demo_graph_edges FORCE ROW LEVEL SECURITY;
CREATE POLICY demo_maintainer_write ON demo_graph_edges FOR ALL USING (COALESCE(current_setting('knowhere.demo_maintainer', true), '') <> '') WITH CHECK (COALESCE(current_setting('knowhere.demo_maintainer', true), '') <> '');
CREATE POLICY demo_public_read ON demo_graph_edges FOR SELECT USING (is_demo_revision_ready(owner_document_id, job_result_id));
ALTER TABLE demo_document_map_unit_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE demo_document_map_unit_tokens FORCE ROW LEVEL SECURITY;
CREATE POLICY demo_maintainer_write ON demo_document_map_unit_tokens FOR ALL USING (COALESCE(current_setting('knowhere.demo_maintainer', true), '') <> '') WITH CHECK (COALESCE(current_setting('knowhere.demo_maintainer', true), '') <> '');
CREATE POLICY demo_public_read ON demo_document_map_unit_tokens FOR SELECT USING (EXISTS (SELECT 1 FROM demo_document_map_units u WHERE u.id = map_unit_id));
