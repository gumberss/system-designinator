using System.Text.Json;
using StackExchange.Redis;

namespace ShortTermRental.Api;

public sealed class PropertyCache(IConnectionMultiplexer redis, BookingStore store, ILogger<PropertyCache> logger)
{
    public async Task<Property?> Get(Guid id, CancellationToken ct)
    {
        var key = $"property:v1:{id}";
        try
        {
            var cached = await redis.GetDatabase().StringGetAsync(key).WaitAsync(ct);
            if (cached.HasValue)
                return JsonSerializer.Deserialize<Property>(cached.ToString())
                    ?? throw new JsonException("Cache de imovel contem null.");
        }
        catch (Exception ex) when (ex is RedisException or TimeoutException or JsonException)
        {
            logger.LogWarning(ex, "Falha ao ler cache {CacheKey}; consultando PostgreSQL", key);
        }
        var property = await store.GetProperty(id, ct);
        if (property is not null)
        {
            try
            {
                await redis.GetDatabase().StringSetAsync(key, JsonSerializer.Serialize(property),
                    TimeSpan.FromSeconds(Random.Shared.Next(240, 301))).WaitAsync(ct);
            }
            catch (Exception ex) when (ex is RedisException or TimeoutException)
            {
                logger.LogWarning(ex, "Falha ao preencher cache {CacheKey}", key);
            }
        }
        return property;
    }
}
