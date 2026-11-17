using Microsoft.AspNetCore.Mvc;
using AiUberMarketplace.Api.Dtos;

namespace AiUberMarketplace.Api.Controllers
{
    /// <summary>
    /// API endpoints for driver operations
    /// </summary>
    [ApiController]
    [Route("api/drivers")]
    public class DriversController : ControllerBase
    {
        private readonly ILogger<DriversController> _logger;

        public DriversController(ILogger<DriversController> logger)
        {
            _logger = logger;
        }

        /// <summary>
        /// Update driver's current location
        /// </summary>
        /// <param name="id">Driver ID</param>
        /// <param name="request">Location update details</param>
        /// <returns>Updated driver location</returns>
        [HttpPut("{id}/location")]
        [ProducesResponseType(typeof(DriverLocationUpdateRequest), StatusCodes.Status200OK)]
        [ProducesResponseType(typeof(ErrorResponseDto), StatusCodes.Status400BadRequest)]
        [ProducesResponseType(typeof(ErrorResponseDto), StatusCodes.Status404NotFound)]
        public async Task<IActionResult> UpdateLocation(Guid id, [FromBody] DriverLocationUpdateRequest request)
        {
            if (request == null)
                return BadRequest(new ErrorResponseDto { Message = "Location request is required", StatusCode = 400 });

            if (request.Latitude < -90 || request.Latitude > 90)
                return BadRequest(new ErrorResponseDto { Message = "Latitude must be between -90 and 90", StatusCode = 400 });

            if (request.Longitude < -180 || request.Longitude > 180)
                return BadRequest(new ErrorResponseDto { Message = "Longitude must be between -180 and 180", StatusCode = 400 });

            _logger.LogInformation($"Updating location for driver {id}: ({request.Latitude}, {request.Longitude})");
            
            // TODO: Call LocationService to update driver location in Redis
            // TODO: Call MatchingService to find nearby riders
            
            return Ok(request);
        }

        /// <summary>
        /// Get driver profile details
        /// </summary>
        /// <param name="id">Driver ID</param>
        /// <returns>Driver details</returns>
        [HttpGet("{id}")]
        [ProducesResponseType(typeof(DriverResponseDto), StatusCodes.Status200OK)]
        [ProducesResponseType(typeof(ErrorResponseDto), StatusCodes.Status404NotFound)]
        public async Task<IActionResult> GetDriver(Guid id)
        {
            _logger.LogInformation($"Fetching driver details for ID: {id}");
            
            // TODO: Query DbContext to get driver details
            
            return NotFound(new ErrorResponseDto { Message = $"Driver {id} not found", StatusCode = 404 });
        }

        /// <summary>
        /// Get driver's trip history
        /// </summary>
        /// <param name="id">Driver ID</param>
        /// <param name="page">Page number (default: 1)</param>
        /// <param name="pageSize">Items per page (default: 10)</param>
        /// <returns>List of completed trips</returns>
        [HttpGet("{id}/trips")]
        [ProducesResponseType(typeof(List<DriverTripHistoryDto>), StatusCodes.Status200OK)]
        [ProducesResponseType(typeof(ErrorResponseDto), StatusCodes.Status404NotFound)]
        public async Task<IActionResult> GetDriverTrips(Guid id, [FromQuery] int page = 1, [FromQuery] int pageSize = 10)
        {
            if (page < 1 || pageSize < 1)
                return BadRequest(new ErrorResponseDto { Message = "Page and pageSize must be greater than 0", StatusCode = 400 });

            _logger.LogInformation($"Fetching trip history for driver {id}, page {page}");
            
            // TODO: Query DbContext for driver's trips with pagination
            
            return Ok(new List<DriverTripHistoryDto>());
        }

        /// <summary>
        /// Toggle driver availability (online/offline)
        /// </summary>
        /// <param name="id">Driver ID</param>
        /// <param name="request">Availability toggle request</param>
        /// <returns>Updated availability status</returns>
        [HttpPost("{id}/availability")]
        [ProducesResponseType(typeof(DriverResponseDto), StatusCodes.Status200OK)]
        [ProducesResponseType(typeof(ErrorResponseDto), StatusCodes.Status400BadRequest)]
        [ProducesResponseType(typeof(ErrorResponseDto), StatusCodes.Status404NotFound)]
        public async Task<IActionResult> ToggleAvailability(Guid id, [FromBody] DriverAvailabilityToggleRequest request)
        {
            if (request == null)
                return BadRequest(new ErrorResponseDto { Message = "Availability request is required", StatusCode = 400 });

            _logger.LogInformation($"Toggling availability for driver {id}: {(request.IsAvailable ? "online" : "offline")}");
            
            // TODO: Update driver availability in database
            // TODO: Update driver location in Redis if going online
            
            return Ok(new DriverResponseDto { IsAvailable = request.IsAvailable });
        }
    }
}
