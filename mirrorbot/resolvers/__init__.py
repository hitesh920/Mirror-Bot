from ..models import Source


async def resolve_source(source: Source) -> Source:
    """Resolver extension point.

    The next build pass will import and split the original repo's shortener
    bypassers/direct-host resolvers into host-specific modules here.
    """
    return source

