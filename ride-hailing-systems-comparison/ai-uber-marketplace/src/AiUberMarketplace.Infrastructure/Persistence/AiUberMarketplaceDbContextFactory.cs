using Microsoft.EntityFrameworkCore;
using Microsoft.EntityFrameworkCore.Design;

namespace AiUberMarketplace.Infrastructure.Persistence
{
    /// <summary>
    /// Design-time factory for DbContext creation during migrations
    /// </summary>
    public class AiUberMarketplaceDbContextFactory : IDesignTimeDbContextFactory<AiUberMarketplaceDbContext>
    {
        public AiUberMarketplaceDbContext CreateDbContext(string[] args)
        {
            var optionsBuilder = new DbContextOptionsBuilder<AiUberMarketplaceDbContext>();
            
            // Use local PostgreSQL connection string
            const string connectionString = "Host=localhost;Port=5432;Database=ai_uber;Username=postgres;Password=postgres;";
            
            optionsBuilder.UseNpgsql(connectionString);
            
            return new AiUberMarketplaceDbContext(optionsBuilder.Options);
        }
    }
}
