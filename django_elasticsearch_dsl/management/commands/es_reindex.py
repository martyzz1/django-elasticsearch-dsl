# from elasticsearch_dsl import Date, Text, Keyword, Document, connections
# Third party libraries
from elasticsearch_dsl import connections
from elasticsearch.exceptions import NotFoundError
from django_elasticsearch_dsl.registries import registry

# Django libraries
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = "Manage elasticsearch index."

    def add_arguments(self, parser):
        parser.add_argument(
            "--models",
            metavar="app[.model]",
            type=str,
            nargs="*",
            help="Specify the model or app to be updated in elasticsearch",
        )

        parser.add_argument(
            "--no-update-alias", action="store_false", dest="alias", help="Do not update the live alias"
        )

        parser.add_argument("--wipe-old-indexes", action="store_true", help="Wipe the old indexes after reindexing")

        parser.add_argument(
            "--refresh-new-indexes",
            action="store_true",
            help="Manually refresh new indexes. Necessary if index.refresh_interval is set to -1",
        )

        parser.add_argument(
            "--no-count",
            action="store_false",
            default=True,
            dest="count",
            help="Do not include a total count in the summary log line",
        )

        parser.add_argument(
            "--index-base-id", action="store", type=str, help="Use the supplied index-base-id for the index prefix"
        )

        parser.add_argument(
            "--alias-wildcard-pattern",
            action="store",
            type=str,
            help="Use the supplied alias_pattern for wildcard searches",
        )
        parser.add_argument(
            "--alias-fixed-pattern",
            action="store",
            type=str,
            help="Use the supplied alias_pattern for targetted index specific GETs",
        )
        parser.set_defaults(
            parallel=getattr(settings, "ELASTICSEARCH_DSL_PARALLEL", False),
            index_base_id=int(timezone.now().timestamp()),
            alias_wildcard_pattern="_r_wildcard",
            alias_fixed_pattern="_r",
            wipe_old_indexes=True,
        )

    def _get_models(self, args):
        """
          Get Models from registry that match the --models args
          """
        if args:
            models = []
            for arg in args:
                arg = arg.lower()
                match_found = False

                for model in registry.get_models():
                    if model._meta.app_label == arg:
                        models.append(model)
                        match_found = True
                    elif "{}.{}".format(model._meta.app_label.lower(), model._meta.model_name.lower()) == arg:
                        models.append(model)
                        match_found = True

                if not match_found:
                    raise CommandError("No model or app named {}".format(arg))
        else:
            models = registry.get_models()

        return set(models)

    def _create_index_templates(self, models, options):
        """
        Create the index template in elasticsearch specifying the mappings and any
        settings to be used. This can be run at any time, ideally at every new code
        deploy.
        """
        index_base_id = options["index_base_id"]
        for doc in registry.get_documents(models):
            self.stdout.write("Creating index template for '{}'".format(doc._index._name))

            pattern = "{0}-{1}-*".format(index_base_id, doc._index._name)

            # create/overwrite an index template
            index_template = doc._index.as_template(doc._index._name, pattern)
            # upload the template into elasticsearch
            # potentially overriding the one already there
            index_template.save()

    def _reindex_as_new(self, es, models, options):
        """
        reindex function that creates a new index for the data. Optionally it also can
        update the alias to point to the latest index (set ``update_alias=False`` to skip).

        Note that while this function is running the application can still perform
        any and all searches without any loss of functionality. It should, however,
        not perform any writes at this time as those might be lost.

        """

        parallel = options["parallel"]
        index_base_id = options["index_base_id"]
        for doc in registry.get_documents(models):
            self.stdout.write(
                "Indexing {} '{}' objects {}".format(
                    doc().get_queryset().count() if options["count"] else "all",
                    doc.django.model.__name__,
                    "(parallel)" if parallel else "",
                )
            )
            qs = doc().get_indexing_queryset()
            doc().update(qs, parallel=parallel, index_base_id=index_base_id)

    def _refresh_new_indexes(self, es, models, options):
        """
        perform n index refresh on all newly created indexes
        """
        pattern = "{0}-*".format(options["index_base_id"])
        es.indices.refresh(index=pattern)

    def _update_alias(self, es, models, options):
        """
        Move the alias from the old index to the new index
        """
        for index in registry.get_indices(models):

            self._update_wildcard_indexes(es, index, options)
            # Must be called second
            self._update_fixed_indexes(es, index, options)

    def _update_wildcard_indexes(self, es, index, options):
        pattern = "{0}-{1}-*".format(options["index_base_id"], index._name)
        alias = "{0}{1}".format(index._name, options["alias_wildcard_pattern"])
        self.stdout.write("Creating wildcard Alias {0} {1} for '{2}'".format(alias, pattern, index._name))
        try:
            old_index_aliases = es.indices.get_alias(name=alias)
            old_indexes = list(old_index_aliases.keys())
            if old_indexes[0].startswith(str(options["index_base_id"])):
                self.stdout.write("Old Indexes also match current index_base_id, skipping Alias update")
            else:

                es.indices.update_aliases(
                    body={
                        "actions": [
                            {"remove": {"alias": alias, "indices": old_indexes}},
                            {"add": {"alias": alias, "index": pattern}},
                        ]
                    }
                )
                if options["wipe_old_indexes"]:
                    if old_indexes[0].startswith(str(options["index_base_id"])):
                        self.stdout.write("Old Indexes also match current index_base_id, skipping wipe_old_indexes")
                    else:
                        es.indices.delete(index=",".join(old_indexes))

        except NotFoundError:
            es.indices.update_aliases(body={"actions": [{"add": {"alias": alias, "index": pattern}},]})

    def _update_fixed_indexes(self, es, index, options):

        """
            This function should create an alias for each individual index created during the indexing process
            e.g.
            <index._name>_<options["alias_fixed_pattern"]>-<doc.get_index_name>
            = <index_base_id>-<index._name>-<doc.get_index_name>

            So we use the Newly created wildcard alias to get a list of indexes that need an alias
        """

        w_alias = "{0}{1}".format(index._name, options["alias_wildcard_pattern"])
        self.stdout.write("Creating fixed Aliases for indexes found in {0} '{1}'".format(w_alias, index._name))
        try:
            current_indexes_for_alias = es.indices.get_alias(name=w_alias)
            current_indexes = list(current_indexes_for_alias.keys())

            if not current_indexes[0].startswith(str(options["index_base_id"])):
                self.stdout.write("Current Indexes don't match the current index_base_id, skipping Alias update")
            else:

                for current_index in current_indexes:
                    index_postfix = current_index.replace("{0}-{1}".format(options["index_base_id"], index._name), "")
                    alias = "{0}{1}{2}".format(index._name, options["alias_fixed_pattern"], index_postfix)
                    self.stdout.write(
                        "Creating fixed Alias {0} for {1} index {2}'".format(alias, index._name, current_index)
                    )
                    try:
                        old_indexes_for_alias = es.indices.get_alias(name=alias)
                        old_indexes = list(old_indexes_for_alias.keys())

                        es.indices.update_aliases(
                            body={
                                "actions": [
                                    {"remove": {"alias": alias, "indices": old_indexes}},
                                    {"add": {"alias": alias, "index": current_index}},
                                ]
                            }
                        )
                    except NotFoundError:
                        es.indices.update_aliases(
                            body={"actions": [{"add": {"alias": alias, "index": current_index}},]}
                        )
        except NotFoundError:
            self.stdout.write("No Existing indexes found for alias '{1}' - Not creating Fixed indexes".format(w_alias))

    def handle(self, *args, **options):
        es = connections.get_connection()
        models = self._get_models(options["models"])
        self._create_index_templates(models, options)
        self._reindex_as_new(es, models, options)

        if options["refresh_new_indexes"]:
            self._refresh_new_indexes(es, models, options)

        if options["alias"]:
            self._update_alias(es, models, options)
