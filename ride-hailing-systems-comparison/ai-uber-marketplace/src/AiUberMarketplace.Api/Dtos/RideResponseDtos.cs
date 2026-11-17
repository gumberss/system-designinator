namespace AiUberMarketplace.Api.Dtos
{
    /// <summary>
    /// Request to update driver location
    /// </summary>
    public class DriverLocationUpdateRequest
    {
        public decimal Latitude { get; set; }
        public decimal Longitude { get; set; }
        public decimal? Heading { get; set; }
        public decimal? Speed { get; set; }
    }

    /// <summary>
    /// Response containing driver details
    /// </summary>
    public class DriverResponseDto
    {
        public Guid Id { get; set; }
        public string FirstName { get; set; }
        public string LastName { get; set; }
        public string Email { get; set; }
        public string PhoneNumber { get; set; }
        public bool IsAvailable { get; set; }
        public decimal? CurrentLatitude { get; set; }
        public decimal? CurrentLongitude { get; set; }
        public decimal AverageRating { get; set; }
        public int TotalTrips { get; set; }
        public DateTime CreatedAt { get; set; }
        public DateTime UpdatedAt { get; set; }
    }

    /// <summary>
    /// Request to toggle driver availability
    /// </summary>
    public class DriverAvailabilityToggleRequest
    {
        public bool IsAvailable { get; set; }
        public decimal? Latitude { get; set; }
        public decimal? Longitude { get; set; }
    }

    /// <summary>
    /// Response for driver trip history
    /// </summary>
    public class DriverTripHistoryDto
    {
        public Guid TripId { get; set; }
        public Guid RiderId { get; set; }
        public string RiderName { get; set; }
        public string Status { get; set; }
        public decimal FinalFare { get; set; }
        public int DurationSeconds { get; set; }
        public DateTime CreatedAt { get; set; }
    }

    /// <summary>
    /// Request to create a new ride
    /// </summary>
    public class RideRequestDto
    {
        public Guid RiderId { get; set; }
        public decimal PickupLatitude { get; set; }
        public decimal PickupLongitude { get; set; }
        public decimal DropoffLatitude { get; set; }
        public decimal DropoffLongitude { get; set; }
        public string PickupAddress { get; set; }
        public string DropoffAddress { get; set; }
    }

    /// <summary>
    /// Response containing ride details
    /// </summary>
    public class RideResponseDto
    {
        public Guid Id { get; set; }
        public Guid RiderId { get; set; }
        public string Status { get; set; }
        public decimal EstimatedFare { get; set; }
        public Guid? AssignedDriverId { get; set; }
        public int OffersCount { get; set; }
        public DateTime CreatedAt { get; set; }
        public DateTime UpdatedAt { get; set; }
    }

    /// <summary>
    /// Request to cancel a ride
    /// </summary>
    public class RideCancelRequest
    {
        public string Reason { get; set; }
    }

    /// <summary>
    /// Request to complete a ride
    /// </summary>
    public class RideCompleteRequest
    {
        public decimal FinalFare { get; set; }
        public decimal DistanceTraveledMeters { get; set; }
        public int DurationSeconds { get; set; }
    }

    /// <summary>
    /// Response containing active offers for a driver
    /// </summary>
    public class DriverOfferDto
    {
        public Guid Id { get; set; }
        public Guid TripId { get; set; }
        public string Status { get; set; }
        public DateTime ExpiresAt { get; set; }
        public string PickupAddress { get; set; }
        public string DropoffAddress { get; set; }
        public decimal EstimatedFare { get; set; }
        public DateTime CreatedAt { get; set; }
    }

    /// <summary>
    /// Request to accept/reject an offer
    /// </summary>
    public class OfferAcceptanceDto
    {
        public bool Accept { get; set; }
    }

    /// <summary>
    /// Response containing surge pricing multiplier
    /// </summary>
    public class SurgeMultiplierResponseDto
    {
        public decimal Multiplier { get; set; }
        public decimal Latitude { get; set; }
        public decimal Longitude { get; set; }
        public int AvailableDrivers { get; set; }
        public int DemandLevel { get; set; } // 1=low, 2=medium, 3=high, 4=very_high
        public DateTime CalculatedAt { get; set; }
    }

    /// <summary>
    /// Generic API error response
    /// </summary>
    public class ErrorResponseDto
    {
        public int StatusCode { get; set; }
        public string Message { get; set; }
        public string? Details { get; set; }
        public DateTime Timestamp { get; set; } = DateTime.UtcNow;
    }
}
