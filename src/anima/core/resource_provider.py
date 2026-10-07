"""Fail-closed defaults for primitive resource providers."""


class ResourceProviderBase:
    async def list_resources(self, collection, *, cursor, limit, context):
        raise PermissionError("operation_not_allowed")

    async def search_resources(self, collection, *, query, limit, context):
        raise PermissionError("operation_not_allowed")

    async def read_resource(self, collection, resource_id, context):
        raise PermissionError("operation_not_allowed")

    async def write_resource(self, collection, resource_id, mode, content, context):
        raise PermissionError("operation_not_allowed")

    async def delete_resource(self, collection, resource_id, context):
        raise PermissionError("operation_not_allowed")

    async def export_resource(self, collection, resource_id, context):
        raise PermissionError("operation_not_allowed")

    async def import_resource(self, collection, resource_id, transfer, context):
        raise PermissionError("operation_not_allowed")
