# utils/helpers.py

class LegacyInteraction:
    def __init__(self, ctx):
        self.guild = ctx.guild
        self.user = ctx.author
        self.channel = ctx.channel

    class Response:
        def __init__(self, ctx):
            self.ctx = ctx

        async def send_message(self, content=None, *, embed=None, ephemeral=False, view=None):
            kwargs = {}
            if content:
                kwargs["content"] = content
            if embed:
                kwargs["embed"] = embed
            if view:
                kwargs["view"] = view
            await self.ctx.send(**kwargs)

    @property
    def response(self):
        return self.Response(self)
