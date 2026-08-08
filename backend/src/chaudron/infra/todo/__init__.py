"""Adapters that hand a shopping list to a task application (ADR-0010).

One adapter is shipped -- Todoist -- and the choice of exactly one is argued in
the ADR rather than left to look like an omission.

Nothing outside this package imports an adapter directly:
:class:`~chaudron.infra.todo.factory.ShoppingExportFactory` resolves a household's
destination the way ``infra/llm/factory.py`` resolves its model provider, and
``services/shopping_export.py`` only ever sees the
:class:`~chaudron.domain.shopping_export.ShoppingListExporter` port.
"""
