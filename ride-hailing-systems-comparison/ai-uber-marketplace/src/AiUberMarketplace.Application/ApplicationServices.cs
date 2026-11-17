using System;
using System.Threading.Tasks;
using AiUberMarketplace.Domain;

namespace AiUberMarketplace.Application.Services
{
    /// <summary>
    /// Service for rider-related operations
    /// </summary>
    public class RiderApplicationService
    {
        private readonly IRiderRepository _riderRepository;

        public RiderApplicationService(IRiderRepository riderRepository)
        {
            _riderRepository = riderRepository ?? throw new ArgumentNullException(nameof(riderRepository));
        }

        public async Task<RiderDto> RegisterRiderAsync(RegisterRiderRequest request)
        {
            if (string.IsNullOrWhiteSpace(request.Email))
                throw new ArgumentException("Email is required", nameof(request.Email));

            if (string.IsNullOrWhiteSpace(request.FirstName))
                throw new ArgumentException("First name is required", nameof(request.FirstName));

            if (string.IsNullOrWhiteSpace(request.LastName))
                throw new ArgumentException("Last name is required", nameof(request.LastName));

            // Create user (in real implementation, would call user service)
            var user = new User(request.Email, request.PhoneNumber, "rider");

            // Create rider aggregate
            var rider = new Rider(
                user.UserId,
                request.FirstName,
                request.LastName,
                request.Email,
                request.PhoneNumber,
                request.RiderType ?? "regular");

            await _riderRepository.AddAsync(rider);

            return new RiderDto
            {
                RiderId = rider.Id,
                Email = rider.Email,
                FirstName = rider.FirstName,
                LastName = rider.LastName,
                CreatedAt = rider.CreatedAt
            };
        }

        public async Task<RiderDto> GetRiderAsync(Guid riderId)
        {
            var rider = await _riderRepository.GetByIdAsync(riderId);
            if (rider == null)
                throw new InvalidOperationException($"Rider {riderId} not found");

            return new RiderDto
            {
                RiderId = rider.Id,
                Email = rider.Email,
                FirstName = rider.FirstName,
                LastName = rider.LastName,
                CreatedAt = rider.CreatedAt
            };
        }
    }

    /// <summary>
    /// Service for driver-related operations
    /// </summary>
    public class DriverApplicationService
    {
        private readonly IDriverRepository _driverRepository;

        public DriverApplicationService(IDriverRepository driverRepository)
        {
            _driverRepository = driverRepository ?? throw new ArgumentNullException(nameof(driverRepository));
        }

        public async Task<DriverDto> RegisterDriverAsync(RegisterDriverRequest request)
        {
            if (string.IsNullOrWhiteSpace(request.Email))
                throw new ArgumentException("Email is required", nameof(request.Email));

            if (string.IsNullOrWhiteSpace(request.LicenseNumber))
                throw new ArgumentException("License number is required", nameof(request.LicenseNumber));

            if (request.LicenseExpiry <= DateTime.UtcNow)
                throw new ArgumentException("License has expired", nameof(request.LicenseExpiry));

            if (request.Vehicle == null)
                throw new ArgumentException("Vehicle is required", nameof(request.Vehicle));

            // Create user
            var user = new User(request.Email, request.PhoneNumber, "driver");

            // Create vehicle
            var vehicle = new Vehicle(
                request.Vehicle.LicensePlate,
                request.Vehicle.Model,
                request.Vehicle.Color,
                request.Vehicle.Year,
                request.Vehicle.Seats ?? 4,
                request.Vehicle.VehicleType ?? "economy",
                request.Vehicle.InsuranceExpiry);

            // Create driver aggregate
            var driver = new Driver(
                user.UserId,
                request.FirstName,
                request.LastName,
                request.Email,
                request.PhoneNumber,
                request.LicenseNumber,
                request.LicenseExpiry,
                vehicle);

            await _driverRepository.AddAsync(driver);

            return new DriverDto
            {
                DriverId = driver.Id,
                Email = driver.Email,
                FirstName = driver.FirstName,
                LastName = driver.LastName,
                IsAvailable = driver.IsAvailable,
                CreatedAt = driver.CreatedAt
            };
        }

        public async Task<DriverDto> GoOnlineAsync(Guid driverId, decimal latitude, decimal longitude)
        {
            var driver = await _driverRepository.GetByIdAsync(driverId);
            if (driver == null)
                throw new InvalidOperationException($"Driver {driverId} not found");

            var location = new Location(latitude, longitude);
            driver.GoOnline(location);

            await _driverRepository.UpdateAsync(driver);

            return new DriverDto
            {
                DriverId = driver.Id,
                Email = driver.Email,
                FirstName = driver.FirstName,
                IsAvailable = driver.IsAvailable,
                CreatedAt = driver.CreatedAt
            };
        }

        public async Task<DriverDto> GoOfflineAsync(Guid driverId)
        {
            var driver = await _driverRepository.GetByIdAsync(driverId);
            if (driver == null)
                throw new InvalidOperationException($"Driver {driverId} not found");

            driver.GoOffline();
            await _driverRepository.UpdateAsync(driver);

            return new DriverDto
            {
                DriverId = driver.Id,
                Email = driver.Email,
                FirstName = driver.FirstName,
                IsAvailable = driver.IsAvailable,
                CreatedAt = driver.CreatedAt
            };
        }

        public async Task<DriverDto> GetDriverAsync(Guid driverId)
        {
            var driver = await _driverRepository.GetByIdAsync(driverId);
            if (driver == null)
                throw new InvalidOperationException($"Driver {driverId} not found");

            return new DriverDto
            {
                DriverId = driver.Id,
                Email = driver.Email,
                FirstName = driver.FirstName,
                IsAvailable = driver.IsAvailable,
                CreatedAt = driver.CreatedAt
            };
        }
    }

    /// <summary>
    /// Service for ride request operations
    /// </summary>
    public class RideRequestApplicationService
    {
        private readonly IRideRequestRepository _rideRepository;
        private readonly IDriverOfferRepository _offerRepository;
        private readonly IDriverRepository _driverRepository;

        public RideRequestApplicationService(
            IRideRequestRepository rideRepository,
            IDriverOfferRepository offerRepository,
            IDriverRepository driverRepository)
        {
            _rideRepository = rideRepository ?? throw new ArgumentNullException(nameof(rideRepository));
            _offerRepository = offerRepository ?? throw new ArgumentNullException(nameof(offerRepository));
            _driverRepository = driverRepository ?? throw new ArgumentNullException(nameof(driverRepository));
        }

        public async Task<RideRequestDto> RequestRideAsync(RequestRideCommand request)
        {
            if (request.PickupLatitude < -90 || request.PickupLatitude > 90)
                throw new ArgumentException("Invalid pickup latitude", nameof(request.PickupLatitude));

            if (request.DropoffLatitude < -90 || request.DropoffLatitude > 90)
                throw new ArgumentException("Invalid dropoff latitude", nameof(request.DropoffLatitude));

            var pickupLocation = new Location(request.PickupLatitude, request.PickupLongitude);
            var dropoffLocation = new Location(request.DropoffLatitude, request.DropoffLongitude);

            var rideRequest = new RideRequest(
                request.RiderId,
                pickupLocation,
                dropoffLocation,
                request.PickupAddress,
                request.DropoffAddress,
                request.EstimatedFare ?? 0);

            await _rideRepository.AddAsync(rideRequest);

            // Query nearby drivers and send offers
            var nearbyDrivers = await _driverRepository.GetAvailableDriversAsync();
            foreach (var driver in nearbyDrivers.Take(10))
            {
                var offer = new DriverOffer(rideRequest.Id, driver.Id, TimeSpan.FromSeconds(20));
                await _offerRepository.AddAsync(offer);
            }

            return new RideRequestDto
            {
                RideId = rideRequest.Id,
                RiderId = rideRequest.RiderId,
                Status = rideRequest.Status,
                EstimatedFare = rideRequest.EstimatedFare,
                CreatedAt = rideRequest.CreatedAt
            };
        }

        public async Task<RideRequestDto> GetRideAsync(Guid rideId)
        {
            var ride = await _rideRepository.GetByIdAsync(rideId);
            if (ride == null)
                throw new InvalidOperationException($"Ride {rideId} not found");

            return new RideRequestDto
            {
                RideId = ride.Id,
                RiderId = ride.RiderId,
                Status = ride.Status,
                EstimatedFare = ride.EstimatedFare,
                AssignedDriverId = ride.AssignedDriverId,
                CreatedAt = ride.CreatedAt
            };
        }
    }

    /// <summary>
    /// Service for trip operations and matching
    /// </summary>
    public class TripApplicationService
    {
        private readonly ITripRepository _tripRepository;
        private readonly IRideRequestRepository _rideRepository;
        private readonly IDriverOfferRepository _offerRepository;

        public TripApplicationService(
            ITripRepository tripRepository,
            IRideRequestRepository rideRepository,
            IDriverOfferRepository offerRepository)
        {
            _tripRepository = tripRepository ?? throw new ArgumentNullException(nameof(tripRepository));
            _rideRepository = rideRepository ?? throw new ArgumentNullException(nameof(rideRepository));
            _offerRepository = offerRepository ?? throw new ArgumentNullException(nameof(offerRepository));
        }

        public async Task<TripDto> AcceptOfferAsync(AcceptOfferCommand request)
        {
            // Verify offer exists and hasn't expired
            var offer = await _offerRepository.GetByIdAsync(request.OfferId);
            if (offer == null)
                throw new InvalidOperationException($"Offer {request.OfferId} not found");

            if (offer.IsExpired())
                throw new InvalidOperationException("Offer has expired");

            if (offer.Status != "sent")
                throw new InvalidOperationException($"Offer is already {offer.Status}");

            // Get ride
            var ride = await _rideRepository.GetByIdAsync(offer.RideId);
            if (ride == null)
                throw new InvalidOperationException($"Ride {offer.RideId} not found");

            if (ride.Status != "requested")
                throw new InvalidOperationException("Ride is no longer available");

            // Try to assign driver (with optimistic locking)
            try
            {
                ride.AssignDriver(offer.DriverId);
                await _rideRepository.UpdateAsync(ride);

                // Accept the offer
                offer.Accept();
                await _offerRepository.UpdateAsync(offer);

                // Create trip
                var trip = new Trip(
                    ride.Id,
                    offer.DriverId,
                    ride.RiderId,
                    request.VehicleId,
                    ride.PickupLocation,
                    ride.DropoffLocation);

                await _tripRepository.AddAsync(trip);

                return new TripDto
                {
                    TripId = trip.Id,
                    RideId = trip.RideId,
                    DriverId = trip.DriverId,
                    Status = trip.Status,
                    CreatedAt = trip.CreatedAt
                };
            }
            catch (InvalidOperationException ex) when (ex.Message.Contains("DbUpdateConcurrencyException"))
            {
                throw new InvalidOperationException("Another driver accepted this ride first");
            }
        }

        public async Task<TripDto> StartTripAsync(Guid tripId)
        {
            var trip = await _tripRepository.GetByIdAsync(tripId);
            if (trip == null)
                throw new InvalidOperationException($"Trip {tripId} not found");

            trip.MarkDriverArrived();
            trip.MarkAsInProgress();
            await _tripRepository.UpdateAsync(trip);

            return new TripDto
            {
                TripId = trip.Id,
                RideId = trip.RideId,
                DriverId = trip.DriverId,
                Status = trip.Status,
                StartedAt = trip.StartedAt,
                CreatedAt = trip.CreatedAt
            };
        }

        public async Task<TripDto> CompleteTripAsync(CompleteTripCommand request)
        {
            var trip = await _tripRepository.GetByIdAsync(request.TripId);
            if (trip == null)
                throw new InvalidOperationException($"Trip {request.TripId} not found");

            trip.Complete(request.FinalFare, request.DistanceTraveled, request.DurationSeconds);
            await _tripRepository.UpdateAsync(trip);

            // Mark ride as completed
            var ride = await _rideRepository.GetByIdAsync(trip.RideId);
            ride.MarkCompleted();
            await _rideRepository.UpdateAsync(ride);

            return new TripDto
            {
                TripId = trip.Id,
                RideId = trip.RideId,
                DriverId = trip.DriverId,
                Status = trip.Status,
                FinalFare = trip.FinalFare,
                DurationSeconds = trip.DurationSeconds,
                CompletedAt = trip.CompletedAt,
                CreatedAt = trip.CreatedAt
            };
        }

        public async Task<TripDto> GetTripAsync(Guid tripId)
        {
            var trip = await _tripRepository.GetByIdAsync(tripId);
            if (trip == null)
                throw new InvalidOperationException($"Trip {tripId} not found");

            return new TripDto
            {
                TripId = trip.Id,
                RideId = trip.RideId,
                DriverId = trip.DriverId,
                RiderId = trip.RiderId,
                Status = trip.Status,
                CreatedAt = trip.CreatedAt
            };
        }
    }

    // ===== DTOs & Commands =====

    public class RegisterRiderRequest
    {
        public string Email { get; set; }
        public string FirstName { get; set; }
        public string LastName { get; set; }
        public string PhoneNumber { get; set; }
        public string RiderType { get; set; }
    }

    public class RegisterDriverRequest
    {
        public string Email { get; set; }
        public string FirstName { get; set; }
        public string LastName { get; set; }
        public string PhoneNumber { get; set; }
        public string LicenseNumber { get; set; }
        public DateTime LicenseExpiry { get; set; }
        public RegisterVehicleRequest Vehicle { get; set; }
    }

    public class RegisterVehicleRequest
    {
        public string LicensePlate { get; set; }
        public string Model { get; set; }
        public string Color { get; set; }
        public int Year { get; set; }
        public int? Seats { get; set; }
        public string VehicleType { get; set; }
        public DateTime InsuranceExpiry { get; set; }
    }

    public class RequestRideCommand
    {
        public Guid RiderId { get; set; }
        public decimal PickupLatitude { get; set; }
        public decimal PickupLongitude { get; set; }
        public decimal DropoffLatitude { get; set; }
        public decimal DropoffLongitude { get; set; }
        public string PickupAddress { get; set; }
        public string DropoffAddress { get; set; }
        public decimal? EstimatedFare { get; set; }
    }

    public class AcceptOfferCommand
    {
        public Guid RideId { get; set; }
        public Guid OfferId { get; set; }
        public Guid DriverId { get; set; }
        public Guid VehicleId { get; set; }
        public Guid IdempotencyKey { get; set; }
    }

    public class CompleteTripCommand
    {
        public Guid TripId { get; set; }
        public Guid DriverId { get; set; }
        public decimal FinalFare { get; set; }
        public decimal DistanceTraveled { get; set; }
        public int DurationSeconds { get; set; }
    }

    // ===== Response DTOs =====

    public class RiderDto
    {
        public Guid RiderId { get; set; }
        public string Email { get; set; }
        public string FirstName { get; set; }
        public string LastName { get; set; }
        public DateTime CreatedAt { get; set; }
    }

    public class DriverDto
    {
        public Guid DriverId { get; set; }
        public string Email { get; set; }
        public string FirstName { get; set; }
        public string LastName { get; set; }
        public bool IsAvailable { get; set; }
        public DateTime CreatedAt { get; set; }
    }

    public class RideRequestDto
    {
        public Guid RideId { get; set; }
        public Guid RiderId { get; set; }
        public string Status { get; set; }
        public decimal EstimatedFare { get; set; }
        public Guid? AssignedDriverId { get; set; }
        public DateTime CreatedAt { get; set; }
    }

    public class TripDto
    {
        public Guid TripId { get; set; }
        public Guid RideId { get; set; }
        public Guid DriverId { get; set; }
        public Guid? RiderId { get; set; }
        public string Status { get; set; }
        public DateTime? StartedAt { get; set; }
        public DateTime? CompletedAt { get; set; }
        public decimal? FinalFare { get; set; }
        public int? DurationSeconds { get; set; }
        public DateTime CreatedAt { get; set; }
    }
}
