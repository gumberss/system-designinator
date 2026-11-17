using AiUberMarketplace.Infrastructure.Persistence;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using StackExchange.Redis;

namespace AiUberMarketplace.Infrastructure.Extensions
{
    /// <summary>
    /// Extension methods for configuring application services
    /// </summary>
    public static class ServiceCollectionExtensions
    {
        /// <summary>
        /// Add all application services to the dependency injection container
        /// </summary>
        public static IServiceCollection AddApplicationServices(this IServiceCollection services, IConfiguration config)
        {
            // Database
            services.AddDbContext<AiUberMarketplaceDbContext>(options =>
                options.UseNpgsql(config.GetConnectionString("DefaultConnection"))
            );

            // Redis
            var redisConnectionString = config.GetConnectionString("Redis");
            if (!string.IsNullOrEmpty(redisConnectionString))
            {
                services.AddSingleton<IConnectionMultiplexer>(sp =>
                    ConnectionMultiplexer.Connect(redisConnectionString));
            }

            // Domain Services
            // These would be registered here when implemented
            // services.AddScoped<ILocationService, RedisLocationService>();
            // services.AddScoped<IMatchingService, MatchingService>();
            // services.AddScoped<ISurgePricingService, SurgePricingService>();

            // Repositories
            // services.AddScoped<IDriverRepository, DriverRepository>();
            // services.AddScoped<IRiderRepository, RiderRepository>();
            // services.AddScoped<ITripRepository, TripRepository>();

            // Application Services
            // services.AddScoped<RiderApplicationService>();
            // services.AddScoped<DriverApplicationService>();
            // services.AddScoped<TripApplicationService>();

            return services;
        }

        /// <summary>
        /// Add CORS configuration
        /// </summary>
        public static IServiceCollection AddCorsConfiguration(this IServiceCollection services)
        {
            services.AddCors(options =>
            {
                options.AddPolicy("AllowAll", builder =>
                {
                    builder.AllowAnyOrigin()
                           .AllowAnyMethod()
                           .AllowAnyHeader();
                });

                options.AddPolicy("AllowFrontend", builder =>
                {
                    builder.WithOrigins("http://localhost:3000", "http://localhost:5173")
                           .AllowAnyMethod()
                           .AllowAnyHeader()
                           .AllowCredentials();
                });
            });

            return services;
        }
    }
}
