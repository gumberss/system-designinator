using System;
using System.Collections.Generic;

namespace AiUberMarketplace.Domain
{
    /// <summary>
    /// Base class for all domain events
    /// </summary>
    public abstract class DomainEvent
    {
        public string EventId { get; set; } = Guid.NewGuid().ToString();
        public DateTime OccurredAt { get; set; } = DateTime.UtcNow;
        public string AggregateId { get; set; }
        public string AggregateType { get; set; }
        public int AggregateVersion { get; set; }
        public string CorrelationId { get; set; } = Guid.NewGuid().ToString();
    }

    /// <summary>
    /// Base class for all aggregate roots
    /// </summary>
    public abstract class AggregateRoot
    {
        private readonly List<DomainEvent> _uncommittedEvents = new();

        public virtual Guid Id { get; protected set; }
        public int Version { get; protected set; } = 0;

        public IReadOnlyList<DomainEvent> GetUncommittedEvents() => _uncommittedEvents.AsReadOnly();

        public void ClearUncommittedEvents() => _uncommittedEvents.Clear();

        protected void RaiseEvent(DomainEvent @event)
        {
            ApplyEvent(@event);
            _uncommittedEvents.Add(@event);
        }

        protected abstract void ApplyEvent(DomainEvent @event);

        public void LoadFromHistory(IEnumerable<DomainEvent> history)
        {
            foreach (var @event in history)
            {
                ApplyEvent(@event);
                Version++;
            }
        }
    }

    // ===== Value Objects =====

    /// <summary>
    /// Represents a geographic location (latitude, longitude)
    /// </summary>
    public class Location
    {
        public decimal Latitude { get; private set; }
        public decimal Longitude { get; private set; }

        public Location(decimal latitude, decimal longitude)
        {
            if (latitude < -90 || latitude > 90)
                throw new InvalidOperationException("Latitude must be between -90 and 90");
            if (longitude < -180 || longitude > 180)
                throw new InvalidOperationException("Longitude must be between -180 and 180");

            Latitude = latitude;
            Longitude = longitude;
        }

        public override bool Equals(object obj)
        {
            if (obj is not Location other) return false;
            return Latitude == other.Latitude && Longitude == other.Longitude;
        }

        public override int GetHashCode() => HashCode.Combine(Latitude, Longitude);

        public override string ToString() => $"({Latitude}, {Longitude})";
    }

    /// <summary>
    /// Represents a vehicle owned by a driver
    /// </summary>
    public class Vehicle
    {
        public Guid VehicleId { get; private set; }
        public string LicensePlate { get; private set; }
        public string Model { get; private set; }
        public string Color { get; private set; }
        public int Year { get; private set; }
        public int Seats { get; private set; }
        public string VehicleType { get; private set; } // "economy", "comfort", "premium"
        public DateTime InsuranceExpiry { get; private set; }
        public bool IsActive { get; private set; }

        public Vehicle(
            string licensePlate,
            string model,
            string color,
            int year,
            int seats,
            string vehicleType,
            DateTime insuranceExpiry)
        {
            VehicleId = Guid.NewGuid();
            LicensePlate = licensePlate ?? throw new ArgumentNullException(nameof(licensePlate));
            Model = model ?? throw new ArgumentNullException(nameof(model));
            Color = color;
            Year = year;
            Seats = seats;
            VehicleType = vehicleType ?? throw new ArgumentNullException(nameof(vehicleType));
            InsuranceExpiry = insuranceExpiry;
            IsActive = true;
        }

        public bool IsInsuranceValid() => InsuranceExpiry > DateTime.UtcNow;
    }

    // ===== Entities & Aggregates =====

    /// <summary>
    /// User entity - shared across Identity, Rider, and Driver contexts
    /// </summary>
    public class User
    {
        public Guid UserId { get; private set; }
        public string Email { get; private set; }
        public string PhoneNumber { get; private set; }
        public string UserType { get; private set; } // "rider", "driver"
        public DateTime CreatedAt { get; private set; }

        public User(string email, string phoneNumber, string userType)
        {
            UserId = Guid.NewGuid();
            Email = email ?? throw new ArgumentNullException(nameof(email));
            PhoneNumber = phoneNumber;
            UserType = userType ?? throw new ArgumentNullException(nameof(userType));
            CreatedAt = DateTime.UtcNow;
        }
    }

    /// <summary>
    /// Rider profile - aggregate root in Rider context
    /// </summary>
    public class Rider : AggregateRoot
    {
        public Guid RiderId { get; private set; }
        public Guid UserId { get; private set; }
        public string FirstName { get; private set; }
        public string LastName { get; private set; }
        public string Email { get; private set; }
        public string PhoneNumber { get; private set; }
        public string RiderType { get; private set; } // "regular", "premium"
        public DateTime CreatedAt { get; private set; }
        public DateTime UpdatedAt { get; private set; }

        private Rider() { } // EF Core

        public Rider(
            Guid userId,
            string firstName,
            string lastName,
            string email,
            string phoneNumber,
            string riderType = "regular")
        {
            Id = RiderId = Guid.NewGuid();
            UserId = userId;
            FirstName = firstName ?? throw new ArgumentNullException(nameof(firstName));
            LastName = lastName ?? throw new ArgumentNullException(nameof(lastName));
            Email = email ?? throw new ArgumentNullException(nameof(email));
            PhoneNumber = phoneNumber;
            RiderType = riderType;
            CreatedAt = DateTime.UtcNow;
            UpdatedAt = DateTime.UtcNow;

            RaiseEvent(new RiderRegisteredEvent
            {
                AggregateId = RiderId.ToString(),
                AggregateType = nameof(Rider),
                RiderId = RiderId,
                UserId = UserId,
                FirstName = FirstName,
                LastName = LastName,
                Email = Email,
                PhoneNumber = PhoneNumber,
                RiderType = RiderType
            });
        }

        protected override void ApplyEvent(DomainEvent @event)
        {
            switch (@event)
            {
                case RiderRegisteredEvent e:
                    Version++;
                    break;
            }
        }
    }

    /// <summary>
    /// Driver profile - aggregate root in Driver context
    /// </summary>
    public class Driver : AggregateRoot
    {
        public Guid DriverId { get; private set; }
        public Guid UserId { get; private set; }
        public string FirstName { get; private set; }
        public string LastName { get; private set; }
        public string Email { get; private set; }
        public string PhoneNumber { get; private set; }
        public string LicenseNumber { get; private set; }
        public DateTime LicenseExpiry { get; private set; }
        public bool IsAvailable { get; private set; }
        public Location CurrentLocation { get; private set; }
        public Vehicle Vehicle { get; private set; }
        public decimal AverageRating { get; private set; } = 0;
        public int TotalTrips { get; private set; } = 0;
        public DateTime CreatedAt { get; private set; }
        public DateTime UpdatedAt { get; private set; }

        private Driver() { } // EF Core

        public Driver(
            Guid userId,
            string firstName,
            string lastName,
            string email,
            string phoneNumber,
            string licenseNumber,
            DateTime licenseExpiry,
            Vehicle vehicle)
        {
            Id = DriverId = Guid.NewGuid();
            UserId = userId;
            FirstName = firstName ?? throw new ArgumentNullException(nameof(firstName));
            LastName = lastName ?? throw new ArgumentNullException(nameof(lastName));
            Email = email ?? throw new ArgumentNullException(nameof(email));
            PhoneNumber = phoneNumber;
            LicenseNumber = licenseNumber ?? throw new ArgumentNullException(nameof(licenseNumber));
            LicenseExpiry = licenseExpiry;
            Vehicle = vehicle ?? throw new ArgumentNullException(nameof(vehicle));
            IsAvailable = false;
            CreatedAt = DateTime.UtcNow;
            UpdatedAt = DateTime.UtcNow;

            RaiseEvent(new DriverRegisteredEvent
            {
                AggregateId = DriverId.ToString(),
                AggregateType = nameof(Driver),
                DriverId = DriverId,
                UserId = UserId,
                FirstName = FirstName,
                LastName = LastName,
                Email = Email,
                PhoneNumber = PhoneNumber,
                LicenseNumber = LicenseNumber,
                LicenseExpiry = LicenseExpiry
            });
        }

        public void GoOnline(Location location)
        {
            if (!LicenseExpiry.Date.IsValidLicense())
                throw new InvalidOperationException("License is expired");

            CurrentLocation = location ?? throw new ArgumentNullException(nameof(location));
            IsAvailable = true;
            UpdatedAt = DateTime.UtcNow;

            RaiseEvent(new DriverAvailabilityChangedEvent
            {
                AggregateId = DriverId.ToString(),
                AggregateType = nameof(Driver),
                DriverId = DriverId,
                Status = "online",
                Latitude = location.Latitude,
                Longitude = location.Longitude
            });
        }

        public void GoOffline()
        {
            IsAvailable = false;
            UpdatedAt = DateTime.UtcNow;

            RaiseEvent(new DriverAvailabilityChangedEvent
            {
                AggregateId = DriverId.ToString(),
                AggregateType = nameof(Driver),
                DriverId = DriverId,
                Status = "offline"
            });
        }

        public void UpdateLocation(Location location)
        {
            CurrentLocation = location ?? throw new ArgumentNullException(nameof(location));
            UpdatedAt = DateTime.UtcNow;
        }

        protected override void ApplyEvent(DomainEvent @event)
        {
            switch (@event)
            {
                case DriverRegisteredEvent e:
                    Version++;
                    break;
                case DriverAvailabilityChangedEvent e:
                    Version++;
                    break;
            }
        }
    }

    /// <summary>
    /// Trip aggregate - represents a single ride from request to completion
    /// </summary>
    public class Trip : AggregateRoot
    {
        public Guid TripId { get; private set; }
        public Guid RideId { get; private set; }
        public Guid DriverId { get; private set; }
        public Guid RiderId { get; private set; }
        public Guid VehicleId { get; private set; }
        public string Status { get; private set; } // assigned, arriving, arrived, in_progress, completed, cancelled

        public Location PickupLocation { get; private set; }
        public Location DropoffLocation { get; private set; }
        public Location CurrentLocation { get; private set; }

        public DateTime? StartedAt { get; private set; }
        public DateTime? CompletedAt { get; private set; }
        public decimal DistanceTraveledMeters { get; private set; }
        public int DurationSeconds { get; private set; }
        public decimal FinalFare { get; private set; }

        public DateTime CreatedAt { get; private set; }
        public DateTime UpdatedAt { get; private set; }

        // Optimistic locking
        public byte[] RowVersion { get; set; }

        private Trip() { } // EF Core

        public Trip(
            Guid rideId,
            Guid driverId,
            Guid riderId,
            Guid vehicleId,
            Location pickupLocation,
            Location dropoffLocation)
        {
            Id = TripId = Guid.NewGuid();
            RideId = rideId;
            DriverId = driverId;
            RiderId = riderId;
            VehicleId = vehicleId;
            PickupLocation = pickupLocation ?? throw new ArgumentNullException(nameof(pickupLocation));
            DropoffLocation = dropoffLocation ?? throw new ArgumentNullException(nameof(dropoffLocation));
            CurrentLocation = pickupLocation;

            Status = "assigned";
            CreatedAt = DateTime.UtcNow;
            UpdatedAt = DateTime.UtcNow;

            RaiseEvent(new TripCreatedEvent
            {
                AggregateId = TripId.ToString(),
                AggregateType = nameof(Trip),
                TripId = TripId,
                RideId = RideId,
                DriverId = DriverId,
                RiderId = RiderId,
                Status = Status
            });
        }

        public void MarkAsInProgress()
        {
            if (Status != "arrived")
                throw new InvalidOperationException($"Cannot start trip from {Status} status");

            Status = "in_progress";
            StartedAt = DateTime.UtcNow;
            UpdatedAt = DateTime.UtcNow;

            RaiseEvent(new TripStartedEvent
            {
                AggregateId = TripId.ToString(),
                AggregateType = nameof(Trip),
                TripId = TripId,
                RideId = RideId,
                DriverId = DriverId,
                RiderId = RiderId,
                StartedAt = StartedAt.Value
            });
        }

        public void Complete(decimal fare, decimal distance, int duration)
        {
            if (Status != "in_progress")
                throw new InvalidOperationException($"Cannot complete trip from {Status} status");

            Status = "completed";
            FinalFare = fare;
            DistanceTraveledMeters = distance;
            DurationSeconds = duration;
            CompletedAt = DateTime.UtcNow;
            UpdatedAt = DateTime.UtcNow;

            RaiseEvent(new TripCompletedEvent
            {
                AggregateId = TripId.ToString(),
                AggregateType = nameof(Trip),
                TripId = TripId,
                RideId = RideId,
                DriverId = DriverId,
                RiderId = RiderId,
                CompletedAt = CompletedAt.Value,
                FinalFare = FinalFare,
                DurationSeconds = DurationSeconds
            });
        }

        public void MarkDriverArrived()
        {
            Status = "arrived";
            UpdatedAt = DateTime.UtcNow;

            RaiseEvent(new DriverArrivedAtPickupEvent
            {
                AggregateId = TripId.ToString(),
                AggregateType = nameof(Trip),
                TripId = TripId,
                RideId = RideId,
                DriverId = DriverId
            });
        }

        protected override void ApplyEvent(DomainEvent @event)
        {
            switch (@event)
            {
                case TripCreatedEvent:
                case TripStartedEvent:
                case TripCompletedEvent:
                case DriverArrivedAtPickupEvent:
                    Version++;
                    break;
            }
        }
    }

    /// <summary>
    /// Ride request - aggregate root representing a rider's request for a ride
    /// </summary>
    public class RideRequest : AggregateRoot
    {
        public Guid RideId { get; private set; }
        public Guid RiderId { get; private set; }
        public Location PickupLocation { get; private set; }
        public Location DropoffLocation { get; private set; }
        public string PickupAddress { get; private set; }
        public string DropoffAddress { get; private set; }
        public string Status { get; private set; } = "requested"; // requested, searching, assigned, completed, cancelled
        public decimal EstimatedFare { get; private set; }
        public Guid? AssignedDriverId { get; private set; }
        public DateTime CreatedAt { get; private set; }
        public DateTime UpdatedAt { get; private set; }
        public DateTime? CancelledAt { get; private set; }

        // Optimistic locking
        public byte[] RowVersion { get; set; }

        private RideRequest() { } // EF Core

        public RideRequest(
            Guid riderId,
            Location pickupLocation,
            Location dropoffLocation,
            string pickupAddress,
            string dropoffAddress,
            decimal estimatedFare)
        {
            Id = RideId = Guid.NewGuid();
            RiderId = riderId;
            PickupLocation = pickupLocation ?? throw new ArgumentNullException(nameof(pickupLocation));
            DropoffLocation = dropoffLocation ?? throw new ArgumentNullException(nameof(dropoffLocation));
            PickupAddress = pickupAddress;
            DropoffAddress = dropoffAddress;
            EstimatedFare = estimatedFare;
            Status = "requested";
            CreatedAt = DateTime.UtcNow;
            UpdatedAt = DateTime.UtcNow;

            RaiseEvent(new RideRequestedEvent
            {
                AggregateId = RideId.ToString(),
                AggregateType = nameof(RideRequest),
                RideId = RideId,
                RiderId = RiderId,
                PickupLatitude = pickupLocation.Latitude,
                PickupLongitude = pickupLocation.Longitude,
                DropoffLatitude = dropoffLocation.Latitude,
                DropoffLongitude = dropoffLocation.Longitude,
                PickupAddress = PickupAddress,
                DropoffAddress = DropoffAddress,
                EstimatedFare = EstimatedFare
            });
        }

        public void AssignDriver(Guid driverId)
        {
            if (Status != "requested")
                throw new InvalidOperationException($"Cannot assign driver to ride in {Status} status");

            AssignedDriverId = driverId;
            Status = "assigned";
            UpdatedAt = DateTime.UtcNow;

            RaiseEvent(new RideAssignedEvent
            {
                AggregateId = RideId.ToString(),
                AggregateType = nameof(RideRequest),
                RideId = RideId,
                DriverId = driverId
            });
        }

        public void MarkCompleted()
        {
            Status = "completed";
            UpdatedAt = DateTime.UtcNow;
        }

        public void Cancel()
        {
            Status = "cancelled";
            CancelledAt = DateTime.UtcNow;
            UpdatedAt = DateTime.UtcNow;
        }

        protected override void ApplyEvent(DomainEvent @event)
        {
            switch (@event)
            {
                case RideRequestedEvent:
                case RideAssignedEvent:
                    Version++;
                    break;
            }
        }
    }

    /// <summary>
    /// Driver offer - a specific driver's offer for a ride
    /// </summary>
    public class DriverOffer : AggregateRoot
    {
        public Guid OfferId { get; private set; }
        public Guid RideId { get; private set; }
        public Guid DriverId { get; private set; }
        public string Status { get; private set; } = "sent"; // sent, accepted, rejected, expired
        public DateTime ExpiresAt { get; private set; }
        public DateTime CreatedAt { get; private set; }
        public DateTime? RespondedAt { get; private set; }

        private DriverOffer() { } // EF Core

        public DriverOffer(
            Guid rideId,
            Guid driverId,
            TimeSpan offerDuration)
        {
            Id = OfferId = Guid.NewGuid();
            RideId = rideId;
            DriverId = driverId;
            Status = "sent";
            ExpiresAt = DateTime.UtcNow.Add(offerDuration);
            CreatedAt = DateTime.UtcNow;

            RaiseEvent(new DriverOfferSentEvent
            {
                AggregateId = OfferId.ToString(),
                AggregateType = nameof(DriverOffer),
                OfferId = OfferId,
                RideId = RideId,
                DriverId = DriverId,
                ExpiresAt = ExpiresAt
            });
        }

        public void Accept()
        {
            if (DateTime.UtcNow > ExpiresAt)
                throw new InvalidOperationException("Offer has expired");

            if (Status != "sent")
                throw new InvalidOperationException($"Cannot accept offer in {Status} status");

            Status = "accepted";
            RespondedAt = DateTime.UtcNow;
        }

        public void Reject()
        {
            Status = "rejected";
            RespondedAt = DateTime.UtcNow;
        }

        public bool IsExpired() => DateTime.UtcNow > ExpiresAt && Status == "sent";

        protected override void ApplyEvent(DomainEvent @event)
        {
            if (@event is DriverOfferSentEvent)
                Version++;
        }
    }

    // ===== Domain Events =====

    public class RiderRegisteredEvent : DomainEvent
    {
        public Guid RiderId { get; set; }
        public Guid UserId { get; set; }
        public string FirstName { get; set; }
        public string LastName { get; set; }
        public string Email { get; set; }
        public string PhoneNumber { get; set; }
        public string RiderType { get; set; }
    }

    public class DriverRegisteredEvent : DomainEvent
    {
        public Guid DriverId { get; set; }
        public Guid UserId { get; set; }
        public string FirstName { get; set; }
        public string LastName { get; set; }
        public string Email { get; set; }
        public string PhoneNumber { get; set; }
        public string LicenseNumber { get; set; }
        public DateTime LicenseExpiry { get; set; }
    }

    public class DriverAvailabilityChangedEvent : DomainEvent
    {
        public Guid DriverId { get; set; }
        public string Status { get; set; }
        public decimal? Latitude { get; set; }
        public decimal? Longitude { get; set; }
    }

    public class RideRequestedEvent : DomainEvent
    {
        public Guid RideId { get; set; }
        public Guid RiderId { get; set; }
        public decimal PickupLatitude { get; set; }
        public decimal PickupLongitude { get; set; }
        public decimal DropoffLatitude { get; set; }
        public decimal DropoffLongitude { get; set; }
        public string PickupAddress { get; set; }
        public string DropoffAddress { get; set; }
        public decimal EstimatedFare { get; set; }
    }

    public class DriverOfferSentEvent : DomainEvent
    {
        public Guid OfferId { get; set; }
        public Guid RideId { get; set; }
        public Guid DriverId { get; set; }
        public DateTime ExpiresAt { get; set; }
    }

    public class RideAssignedEvent : DomainEvent
    {
        public Guid RideId { get; set; }
        public Guid DriverId { get; set; }
    }

    public class TripCreatedEvent : DomainEvent
    {
        public Guid TripId { get; set; }
        public Guid RideId { get; set; }
        public Guid DriverId { get; set; }
        public Guid RiderId { get; set; }
        public string Status { get; set; }
    }

    public class TripStartedEvent : DomainEvent
    {
        public Guid TripId { get; set; }
        public Guid RideId { get; set; }
        public Guid DriverId { get; set; }
        public Guid RiderId { get; set; }
        public DateTime StartedAt { get; set; }
    }

    public class TripCompletedEvent : DomainEvent
    {
        public Guid TripId { get; set; }
        public Guid RideId { get; set; }
        public Guid DriverId { get; set; }
        public Guid RiderId { get; set; }
        public DateTime CompletedAt { get; set; }
        public decimal FinalFare { get; set; }
        public int DurationSeconds { get; set; }
    }

    public class DriverArrivedAtPickupEvent : DomainEvent
    {
        public Guid TripId { get; set; }
        public Guid RideId { get; set; }
        public Guid DriverId { get; set; }
    }

    // ===== Repository Interfaces =====

    public interface IRiderRepository
    {
        Task AddAsync(Rider rider);
        Task<Rider> GetByIdAsync(Guid riderId);
        Task<Rider> GetByUserIdAsync(Guid userId);
        Task UpdateAsync(Rider rider);
    }

    public interface IDriverRepository
    {
        Task AddAsync(Driver driver);
        Task<Driver> GetByIdAsync(Guid driverId);
        Task<Driver> GetByUserIdAsync(Guid userId);
        Task UpdateAsync(Driver driver);
        Task<List<Driver>> GetAvailableDriversAsync();
    }

    public interface IRideRequestRepository
    {
        Task AddAsync(RideRequest ride);
        Task<RideRequest> GetByIdAsync(Guid rideId);
        Task UpdateAsync(RideRequest ride);
    }

    public interface ITripRepository
    {
        Task AddAsync(Trip trip);
        Task<Trip> GetByIdAsync(Guid tripId);
        Task<Trip> GetByRideIdAsync(Guid rideId);
        Task UpdateAsync(Trip trip);
    }

    public interface IDriverOfferRepository
    {
        Task AddAsync(DriverOffer offer);
        Task<DriverOffer> GetByIdAsync(Guid offerId);
        Task<List<DriverOffer>> GetByRideIdAsync(Guid rideId);
        Task UpdateAsync(DriverOffer offer);
    }

    // ===== Payment Aggregate =====

    /// <summary>
    /// Payment aggregate representing a financial transaction for a trip
    /// </summary>
    public class Payment : AggregateRoot
    {
        public Guid PaymentId { get; private set; }
        public Guid TripId { get; private set; }
        public decimal Amount { get; private set; }
        public string PaymentMethod { get; private set; } // "credit_card", "debit_card", "cash", "wallet"
        public string Status { get; private set; } = "pending"; // pending, completed, failed, refunded
        public string TransactionId { get; private set; }
        public DateTime CreatedAt { get; private set; }
        public DateTime UpdatedAt { get; private set; }

        private Payment() { } // EF Core

        public Payment(Guid tripId, decimal amount, string paymentMethod)
        {
            Id = PaymentId = Guid.NewGuid();
            TripId = tripId;
            Amount = amount;
            PaymentMethod = paymentMethod ?? throw new ArgumentNullException(nameof(paymentMethod));
            Status = "pending";
            CreatedAt = DateTime.UtcNow;
            UpdatedAt = DateTime.UtcNow;
        }

        public void CompletePayment(string transactionId)
        {
            if (Status != "pending")
                throw new InvalidOperationException($"Cannot complete payment in {Status} status");

            TransactionId = transactionId ?? throw new ArgumentNullException(nameof(transactionId));
            Status = "completed";
            UpdatedAt = DateTime.UtcNow;
        }

        public void FailPayment()
        {
            Status = "failed";
            UpdatedAt = DateTime.UtcNow;
        }

        public void Refund()
        {
            Status = "refunded";
            UpdatedAt = DateTime.UtcNow;
        }

        protected override void ApplyEvent(DomainEvent @event)
        {
            // Payment events will be added in future iterations
        }
    }

    // ===== Offer Aggregate =====

    /// <summary>
    /// Offer represents a driver's offer for a specific ride request
    /// </summary>
    public class Offer : AggregateRoot
    {
        public Guid OfferId { get; private set; }
        public Guid TripId { get; private set; }
        public Guid DriverId { get; private set; }
        public DateTime ExpiresAt { get; private set; }
        public string Status { get; private set; } = "pending"; // pending, accepted, rejected, expired
        public DateTime CreatedAt { get; private set; }
        public DateTime? UpdatedAt { get; private set; }

        private Offer() { } // EF Core

        public Offer(Guid tripId, Guid driverId, DateTime expiresAt)
        {
            Id = OfferId = Guid.NewGuid();
            TripId = tripId;
            DriverId = driverId;
            ExpiresAt = expiresAt;
            Status = "pending";
            CreatedAt = DateTime.UtcNow;
        }

        public void Accept()
        {
            if (Status != "pending")
                throw new InvalidOperationException($"Cannot accept offer in {Status} status");
            if (DateTime.UtcNow > ExpiresAt)
                throw new InvalidOperationException("Offer has expired");

            Status = "accepted";
            UpdatedAt = DateTime.UtcNow;
        }

        public void Reject()
        {
            if (Status != "pending")
                throw new InvalidOperationException($"Cannot reject offer in {Status} status");

            Status = "rejected";
            UpdatedAt = DateTime.UtcNow;
        }

        public void Expire()
        {
            if (Status == "pending" && DateTime.UtcNow > ExpiresAt)
            {
                Status = "expired";
                UpdatedAt = DateTime.UtcNow;
            }
        }

        public bool IsActive() => Status == "pending" && DateTime.UtcNow <= ExpiresAt;

        protected override void ApplyEvent(DomainEvent @event)
        {
            // Offer events will be added in future iterations
        }
    }

    // ===== LocationHistory Entity =====

    /// <summary>
    /// LocationHistory tracks driver location updates over time for analytics and replay
    /// </summary>
    public class LocationHistory
    {
        public Guid Id { get; private set; }
        public Guid DriverId { get; private set; }
        public decimal Latitude { get; private set; }
        public decimal Longitude { get; private set; }
        public decimal? Heading { get; private set; }
        public decimal? Speed { get; private set; }
        public DateTime Timestamp { get; private set; }

        private LocationHistory() { } // EF Core

        public LocationHistory(Guid driverId, decimal latitude, decimal longitude, decimal? heading = null, decimal? speed = null)
        {
            Id = Guid.NewGuid();
            DriverId = driverId;
            Latitude = latitude;
            Longitude = longitude;
            Heading = heading;
            Speed = speed;
            Timestamp = DateTime.UtcNow;
        }
    }

    // ===== OutboxEvent Entity =====

    /// <summary>
    /// OutboxEvent implements the transactional outbox pattern for reliable event publishing
    /// Events are written to the database in the same transaction as domain changes,
    /// then published asynchronously by a background worker.
    /// </summary>
    public class OutboxEvent
    {
        public Guid Id { get; private set; }
        public string EventType { get; private set; }
        public string Payload { get; private set; }
        public DateTime CreatedAt { get; private set; }
        public DateTime? ProcessedAt { get; private set; }

        private OutboxEvent() { } // EF Core

        public OutboxEvent(string eventType, string payload)
        {
            Id = Guid.NewGuid();
            EventType = eventType ?? throw new ArgumentNullException(nameof(eventType));
            Payload = payload ?? throw new ArgumentNullException(nameof(payload));
            CreatedAt = DateTime.UtcNow;
        }

        public void MarkAsProcessed()
        {
            ProcessedAt = DateTime.UtcNow;
        }
    }
}
