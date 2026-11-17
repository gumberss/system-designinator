using System;

namespace AiUberMarketplace.Domain
{
    /// <summary>
    /// Extension methods for domain logic
    /// </summary>
    public static class DomainExtensions
    {
        public static bool IsValidLicense(this DateTime licenseExpiry)
        {
            return licenseExpiry.Date >= DateTime.UtcNow.Date;
        }
    }
}
