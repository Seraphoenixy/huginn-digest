"""在表单解析前限制HTTP请求体；不依赖Content-Length声明。"""
from starlette.responses import JSONResponse


class RequestBodyLimitMiddleware:
    def __init__(self, app, max_bytes):
        self.app = app
        self.max_bytes = max_bytes

    async def reject(self, scope, receive, send, status, detail):
        response = JSONResponse({'detail': detail}, status_code=status,
                                headers={'Cache-Control': 'no-store'})
        await response(scope, receive, send)

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        lengths = [value for key, value in scope.get('headers', []) if key.lower() == b'content-length']
        if lengths:
            value = lengths[0]
            if len(lengths) != 1 or len(value) > 20 or not value.isdigit():
                return await self.reject(scope, receive, send, 400, '请求长度声明无效')
            if int(value) > self.max_bytes:
                return await self.reject(scope, receive, send, 413, '请求内容超过大小限制')

        # 有上限地缓存，确认整份请求合规后才交给表单解析器和路由。
        body = bytearray()
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            chunk = message.get('body', b'')
            if len(body) + len(chunk) > self.max_bytes:
                return await self.reject(scope, receive, send, 413, '请求内容超过大小限制')
            body.extend(chunk)
            if not message.get('more_body', False):
                break

        consumed = False

        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
            return await receive()

        await self.app(scope, replay, send)
