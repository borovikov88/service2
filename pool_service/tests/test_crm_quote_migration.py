"""Additive proposal schema leaves existing calculations and commercial data intact."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class QuoteMigrationTests(TransactionTestCase):
    def test_existing_version_and_deal_preserved_without_backfill(self):
        before=[('pool_service','0162_service_document_bindings')]
        after=[('pool_service','0163_crm_quote_lifecycle')]
        executor=MigrationExecutor(connection);executor.migrate(before)
        apps=executor.loader.project_state(before).apps
        org=apps.get_model('pool_service','Organization').objects.create(name='Synthetic migration quote')
        estimate=apps.get_model('pool_service','PoolEstimate').objects.create(organization_id=org.pk,title='Frozen synthetic',revision=1)
        version=apps.get_model('pool_service','PoolEstimateVersion').objects.create(estimate_id=estimate.pk,number=1,snapshot={'synthetic':'unchanged'})
        pipeline=apps.get_model('pool_service','CrmPipeline').objects.create(organization_id=org.pk,code='test',name='Synthetic')
        stage=apps.get_model('pool_service','CrmStage').objects.create(pipeline_id=pipeline.pk,code='test',name='Synthetic')
        deal=apps.get_model('pool_service','CrmDeal').objects.create(organization_id=org.pk,pipeline_id=pipeline.pk,stage_id=stage.pk,title='Preserved synthetic')
        old_version=apps.get_model('pool_service','PoolEstimateVersion').objects.filter(pk=version.pk).values().get()
        old_deal=apps.get_model('pool_service','CrmDeal').objects.filter(pk=deal.pk).values().get()
        try:
            executor=MigrationExecutor(connection);executor.migrate(after)
            apps=executor.loader.project_state(after).apps
            self.assertEqual(old_version,apps.get_model('pool_service','PoolEstimateVersion').objects.filter(pk=version.pk).values().get())
            self.assertEqual(old_deal,apps.get_model('pool_service','CrmDeal').objects.filter(pk=deal.pk).values().get())
            self.assertEqual(apps.get_model('pool_service','CrmQuote').objects.count(),0)
            self.assertEqual(apps.get_model('pool_service','CrmQuoteEvent').objects.count(),0)
        finally:
            executor=MigrationExecutor(connection);executor.migrate(executor.loader.graph.leaf_nodes())
