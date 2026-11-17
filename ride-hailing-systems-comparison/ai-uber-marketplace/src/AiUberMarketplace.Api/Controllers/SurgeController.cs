using Microsoft.AspNetCore.Mvc;
using AiUberMarketplace.Api.Dtos;

namespace AiUberMarketplace.Api.Controllers
{
    /// <summary>
    /// API endpoints for surge pricing queries
    /// </summary>
    [ApiController]
    [Route("api/surge")]
    public class SurgeController : ControllerBase
    {
        private readonly ILogger<SurgeController> _logger;

        public SurgeController(ILogger<SurgeController> logger)
        {
            _logger = logger;
        }

        /// <summary>
        /// Get current surge multiplier for a location
        /// </summary>
        /// <param name="latitude">Location latitude</param>
        /// <param name="longitude">Location longitude</param>
        /// <returns>Current surge multiplier and demand information</returns>
        [HttpGet("multiplier")]
        [ProducesResponseType(typeof(SurgeMultiplierResponseDto), StatusCodes.Status200OK)]
        [ProducesResponseType(typeof(ErrorResponseDto), StatusCodes.Status400BadRequest)]
        public async Task<IActionResult> GetSurgeMultiplier([FromQuery] decimal latitude, [FromQuery] decimal longitude)
        {
            if (latitude < -90 || latitude > 90)
                return BadRequest(new ErrorResponseDto { Message = "Latitude must be between -90 and 90", StatusCode = 400 });

            if (longitude < -180 || longitude > 180)
                return BadRequest(new ErrorResponseDto { Message = "Longitude must be between -180 and 180", StatusCode = 400 });

            _logger.LogInformation($"Calculating surge multiplier for location ({latitude}, {longitude})");
            
            // TODO: Call SurgePricingService to calculate multiplier
            // TODO: Get available drivers count from LocationService
            // TODO: Calculate demand level based on active rides and available drivers
            
            var response = new SurgeMultiplierResponseDto
            {
                Multiplier = 1.0m,
                Latitude = latitude,
                Longitude = longitude,
                AvailableDrivers = 0,
                DemandLevel = 1,
                CalculatedAt = DateTime.UtcNow
            };

            return Ok(response);
        }
    }
}
