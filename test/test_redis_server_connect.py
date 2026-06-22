import redis.asyncio as redis

async def test_redis_connection():
    client = redis.Redis(host="localhost", port=6379, decode_responses=True)
    await client.ping()
    print("Redis connection successful!")

import asyncio
asyncio.run(test_redis_connection())