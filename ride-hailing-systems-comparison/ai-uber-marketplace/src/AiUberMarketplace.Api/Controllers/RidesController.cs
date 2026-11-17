using Microsoft.AspNetCore.Mvc;
using AiUberMarketplace.Api.Dtos;

namespace AiUberMarketplace.Api.Controllers
{
    /// <summary>
    /// API endpoints for ride operations
    /// </summary>
    [ApiController]
    [Route("api/rides")]
    public class RidesController : ControllerBase
    {
        private readonly ILogger<RidesController> _logger;

        public RidesController(ILogger<RidesController> logger)
        {
            _logger = logger;
        }

        /// <summary>
        /// Create a new ride request
        /// </summary>
        /// <param name="request">Ride request details</param>
        /// <returns>Created ride details with available offers</returns>
        [HttpPost]
        [ProducesResponseType(typeof(RideResponseDto), StatusCodes.Status201Created)]
        [ProducesResponseType(typeof(ErrorResponseDto), StatusCodes.Status400BadRequest)]
        public async Task<IActionResult> CreateRide([FromBody] RideRequestDto request)
        {
            if (request == null)
                return BadRequest(new ErrorResponseDto { Message = "Ride request is required", StatusCode = 400 });

            if (request.RiderId == Guid.Empty)
                return BadRequest(new ErrorResponseDto { Message = "Rider ID is required", StatusCode = 400 });

            if (request.PickupLatitude == request.DropoffLatitude && request.PickupLongitude == request.DropoffLongitude)
                return BadRequest(new ErrorResponseDto { Message = "Pickup and dropoff locations cannot be the same", StatusCode = 400 });

            _logger.LogInformation($"Creating ride for rider {request.RiderId} from ({request.PickupLatitude}, {request.PickupLongitude}) to ({request.DropoffLatitude}, {request.DropoffLongitude})");
            
            // TODO: Create ride request aggregate
            // TODO: Call MatchingService to find nearby drivers
            // TODO: Create offers for available drivers
            // TODO: Return ride with offer count
            
            var ride = new RideResponseDto
            {
                Id = Guid.NewGuid(),
                RiderId = request.RiderId,
                Status = "requested",
                EstimatedFare = 15.50m,
                OffersCount = 0,
                CreatedAt = DateTime.UtcNow,
                UpdatedAt = DateTime.UtcNow
            };

            return CreatedAtAction(nameof(GetRide), new { id = ride.Id }, ride);
        }

        /// <summary>
        /// Get ride details by ID
        /// </summary>
        /// <param name="id">Ride ID</param>
        /// <returns>Ride details</returns>
        [HttpGet("{id}")]
        [ProducesResponseType(typeof(RideResponseDto), StatusCodes.Status200OK)]
        [ProducesResponseType(typeof(ErrorResponseDto), StatusCodes.Status404NotFound)]
        public async Task<IActionResult> GetRide(Guid id)
        {
            _logger.LogInformation($"Fetching ride details for ID: {id}");
            
            // TODO: Query DbContext for ride details
            
            return NotFound(new ErrorResponseDto { Message = $"Ride {id} not found", StatusCode = 404 });
        }

        /// <summary>
        /// Get rider's rides (completed and active)
        /// </summary>
        /// <param name="riderId">Rider ID</param>
        /// <param name="status">Filter by status (optional)</param>
        /// <param name="page">Page number (default: 1)</param>
        /// <param name="pageSize">Items per page (default: 10)</param>
        /// <returns>List of rides</returns>
        [HttpGet]
        [ProducesResponseType(typeof(List<RideResponseDto>), StatusCodes.Status200OK)]
        [ProducesResponseType(typeof(ErrorResponseDto), StatusCodes.Status400BadRequest)]
        public async Task<IActionResult> GetRiderRides([FromQuery] Guid riderId, [FromQuery] string? status = null, [FromQuery] int page = 1, [FromQuery] int pageSize = 10)
        {
            if (riderId == Guid.Empty)
                return BadRequest(new ErrorResponseDto { Message = "Rider ID is required", StatusCode = 400 });

            if (page < 1 || pageSize < 1)
                return BadRequest(new ErrorResponseDto { Message = "Page and pageSize must be greater than 0", StatusCode = 400 });

            _logger.LogInformation($"Fetching rides for rider {riderId}, status={status}, page={page}");
            
            // TODO: Query DbContext for rider's rides with filtering and pagination
            
            return Ok(new List<RideResponseDto>());
        }

        /// <summary>
        /// Cancel an active ride
        /// </summary>
        /// <param name="id">Ride ID</param>
        /// <param name="request">Cancellation reason</param>
        /// <returns>Cancelled ride details</returns>
        [HttpPut("{id}/cancel")]
        [ProducesResponseType(typeof(RideResponseDto), StatusCodes.Status200OK)]
        [ProducesResponseType(typeof(ErrorResponseDto), StatusCodes.Status400BadRequest)]
        [ProducesResponseType(typeof(ErrorResponseDto), StatusCodes.Status404NotFound)]
        public async Task<IActionResult> CancelRide(Guid id, [FromBody] RideCancelRequest request)
        {
            if (request == null || string.IsNullOrWhiteSpace(request.Reason))
                return BadRequest(new ErrorResponseDto { Message = "Cancellation reason is required", StatusCode = 400 });

            _logger.LogInformation($"Cancelling ride {id}, reason: {request.Reason}");
            
            // TODO: Update ride status to cancelled
            // TODO: Notify assigned driver if any
            // TODO: Handle payment/refund logic
            
            return NotFound(new ErrorResponseDto { Message = $"Ride {id} not found", StatusCode = 404 });
        }

        /// <summary>
        /// Complete a trip and finalize payment
        /// </summary>
        /// <param name="id">Ride ID</param>
        /// <param name="request">Final ride details (fare, distance, duration)</param>
        /// <returns>Completed ride details</returns>
        [HttpPut("{id}/complete")]
        [ProducesResponseType(typeof(RideResponseDto), StatusCodes.Status200OK)]
        [ProducesResponseType(typeof(ErrorResponseDto), StatusCodes.Status400BadRequest)]
        [ProducesResponseType(typeof(ErrorResponseDto), StatusCodes.Status404NotFound)]
        public async Task<IActionResult> CompleteRide(Guid id, [FromBody] RideCompleteRequest request)
        {
            if (request == null)
                return BadRequest(new ErrorResponseDto { Message = "Completion details are required", StatusCode = 400 });

            if (request.FinalFare <= 0 || request.DistanceTraveledMeters < 0 || request.DurationSeconds < 0)
                return BadRequest(new ErrorResponseDto { Message = "Invalid ride completion details", StatusCode = 400 });

            _logger.LogInformation($"Completing ride {id}, fare: ${request.FinalFare}, distance: {request.DistanceTraveledMeters}m, duration: {request.DurationSeconds}s");
            
            // TODO: Update trip status to completed
            // TODO: Create payment record
            // TODO: Update driver/rider ratings
            
            return NotFound(new ErrorResponseDto { Message = $"Ride {id} not found", StatusCode = 404 });
        }
    }
}
