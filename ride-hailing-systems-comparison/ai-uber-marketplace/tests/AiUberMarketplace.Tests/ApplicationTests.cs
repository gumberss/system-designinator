using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using Xunit;
using Moq;
using AiUberMarketplace.Domain;
using AiUberMarketplace.Application.Services;

namespace AiUberMarketplace.Tests.Application
{
    public class RiderApplicationServiceTests
    {
        private readonly Mock<IRiderRepository> _mockRepository;
        private readonly RiderApplicationService _service;

        public RiderApplicationServiceTests()
        {
            _mockRepository = new Mock<IRiderRepository>();
            _service = new RiderApplicationService(_mockRepository.Object);
        }

        [Fact]
        public async Task RegisterRiderAsync_WithValidRequest_ShouldReturnRiderDto()
        {
            // Arrange
            var request = new RegisterRiderRequest
            {
                Email = "joao@example.com",
                FirstName = "João",
                LastName = "Silva",
                PhoneNumber = "11999999999",
                RiderType = "regular"
            };

            _mockRepository.Setup(r => r.AddAsync(It.IsAny<Rider>())).Returns(Task.CompletedTask);

            // Act
            var result = await _service.RegisterRiderAsync(request);

            // Assert
            Assert.NotNull(result);
            Assert.Equal(request.Email, result.Email);
            Assert.Equal(request.FirstName, result.FirstName);
            Assert.Equal(request.LastName, result.LastName);
            _mockRepository.Verify(r => r.AddAsync(It.IsAny<Rider>()), Times.Once);
        }

        [Fact]
        public async Task RegisterRiderAsync_WithoutEmail_ShouldThrow()
        {
            // Arrange
            var request = new RegisterRiderRequest
            {
                Email = null,
                FirstName = "João",
                LastName = "Silva"
            };

            // Act & Assert
            await Assert.ThrowsAsync<ArgumentException>(() => _service.RegisterRiderAsync(request));
        }

        [Fact]
        public async Task GetRiderAsync_WithExistingRider_ShouldReturnRider()
        {
            // Arrange
            var riderId = Guid.NewGuid();
            var rider = new Rider(
                Guid.NewGuid(),
                "João",
                "Silva",
                "joao@example.com",
                "11999999999");

            _mockRepository.Setup(r => r.GetByIdAsync(riderId)).ReturnsAsync(rider);

            // Act
            var result = await _service.GetRiderAsync(riderId);

            // Assert
            Assert.NotNull(result);
            Assert.Equal("João", result.FirstName);
        }

        [Fact]
        public async Task GetRiderAsync_WithNonExistentRider_ShouldThrow()
        {
            // Arrange
            var riderId = Guid.NewGuid();
            _mockRepository.Setup(r => r.GetByIdAsync(riderId)).ReturnsAsync((Rider)null);

            // Act & Assert
            await Assert.ThrowsAsync<InvalidOperationException>(() => _service.GetRiderAsync(riderId));
        }
    }

    public class DriverApplicationServiceTests
    {
        private readonly Mock<IDriverRepository> _mockRepository;
        private readonly DriverApplicationService _service;

        public DriverApplicationServiceTests()
        {
            _mockRepository = new Mock<IDriverRepository>();
            _service = new DriverApplicationService(_mockRepository.Object);
        }

        [Fact]
        public async Task RegisterDriverAsync_WithValidRequest_ShouldReturnDriverDto()
        {
            // Arrange
            var request = new RegisterDriverRequest
            {
                Email = "maria@example.com",
                FirstName = "Maria",
                LastName = "Santos",
                PhoneNumber = "11988888888",
                LicenseNumber = "DL123456",
                LicenseExpiry = DateTime.UtcNow.AddYears(2),
                Vehicle = new RegisterVehicleRequest
                {
                    LicensePlate = "ABC1234",
                    Model = "Toyota Corolla",
                    Color = "White",
                    Year = 2022,
                    Seats = 4,
                    VehicleType = "economy",
                    InsuranceExpiry = DateTime.UtcNow.AddYears(1)
                }
            };

            _mockRepository.Setup(r => r.AddAsync(It.IsAny<Driver>())).Returns(Task.CompletedTask);

            // Act
            var result = await _service.RegisterDriverAsync(request);

            // Assert
            Assert.NotNull(result);
            Assert.Equal(request.Email, result.Email);
            Assert.Equal(request.FirstName, result.FirstName);
            Assert.False(result.IsAvailable);
            _mockRepository.Verify(r => r.AddAsync(It.IsAny<Driver>()), Times.Once);
        }

        [Fact]
        public async Task RegisterDriverAsync_WithExpiredLicense_ShouldThrow()
        {
            // Arrange
            var request = new RegisterDriverRequest
            {
                Email = "maria@example.com",
                FirstName = "Maria",
                LastName = "Santos",
                LicenseNumber = "DL123456",
                LicenseExpiry = DateTime.UtcNow.AddDays(-1),
                Vehicle = new RegisterVehicleRequest
                {
                    LicensePlate = "ABC1234",
                    Model = "Toyota",
                    InsuranceExpiry = DateTime.UtcNow.AddYears(1)
                }
            };

            // Act & Assert
            await Assert.ThrowsAsync<ArgumentException>(() => _service.RegisterDriverAsync(request));
        }

        [Fact]
        public async Task GoOnlineAsync_WithValidLocation_ShouldMarkAvailable()
        {
            // Arrange
            var driverId = Guid.NewGuid();
            var vehicle = new Vehicle("ABC1234", "Toyota", "White", 2022, 4, "economy", DateTime.UtcNow.AddYears(1));
            var driver = new Driver(
                Guid.NewGuid(),
                "Maria",
                "Santos",
                "maria@example.com",
                "11988888888",
                "DL123456",
                DateTime.UtcNow.AddYears(2),
                vehicle);

            _mockRepository.Setup(r => r.GetByIdAsync(driverId)).ReturnsAsync(driver);
            _mockRepository.Setup(r => r.UpdateAsync(It.IsAny<Driver>())).Returns(Task.CompletedTask);

            // Act
            var result = await _service.GoOnlineAsync(driverId, -23.5505m, -46.6333m);

            // Assert
            Assert.NotNull(result);
            Assert.True(result.IsAvailable);
            _mockRepository.Verify(r => r.UpdateAsync(It.IsAny<Driver>()), Times.Once);
        }

        [Fact]
        public async Task GoOfflineAsync_WithOnlineDriver_ShouldMarkUnavailable()
        {
            // Arrange
            var driverId = Guid.NewGuid();
            var vehicle = new Vehicle("ABC1234", "Toyota", "White", 2022, 4, "economy", DateTime.UtcNow.AddYears(1));
            var driver = new Driver(
                Guid.NewGuid(),
                "Maria",
                "Santos",
                "maria@example.com",
                "11988888888",
                "DL123456",
                DateTime.UtcNow.AddYears(2),
                vehicle);

            driver.GoOnline(new Location(-23.5505m, -46.6333m));

            _mockRepository.Setup(r => r.GetByIdAsync(driverId)).ReturnsAsync(driver);
            _mockRepository.Setup(r => r.UpdateAsync(It.IsAny<Driver>())).Returns(Task.CompletedTask);

            // Act
            var result = await _service.GoOfflineAsync(driverId);

            // Assert
            Assert.NotNull(result);
            Assert.False(result.IsAvailable);
            _mockRepository.Verify(r => r.UpdateAsync(It.IsAny<Driver>()), Times.Once);
        }
    }

    public class RideRequestApplicationServiceTests
    {
        private readonly Mock<IRideRequestRepository> _mockRideRepository;
        private readonly Mock<IDriverOfferRepository> _mockOfferRepository;
        private readonly Mock<IDriverRepository> _mockDriverRepository;
        private readonly RideRequestApplicationService _service;

        public RideRequestApplicationServiceTests()
        {
            _mockRideRepository = new Mock<IRideRequestRepository>();
            _mockOfferRepository = new Mock<IDriverOfferRepository>();
            _mockDriverRepository = new Mock<IDriverRepository>();
            _service = new RideRequestApplicationService(
                _mockRideRepository.Object,
                _mockOfferRepository.Object,
                _mockDriverRepository.Object);
        }

        [Fact]
        public async Task RequestRideAsync_WithValidRequest_ShouldCreateRide()
        {
            // Arrange
            var request = new RequestRideCommand
            {
                RiderId = Guid.NewGuid(),
                PickupLatitude = -23.5505m,
                PickupLongitude = -46.6333m,
                DropoffLatitude = -23.5475m,
                DropoffLongitude = -46.6361m,
                PickupAddress = "Av Paulista, 100",
                DropoffAddress = "Rua Augusta, 200",
                EstimatedFare = 50m
            };

            _mockRideRepository.Setup(r => r.AddAsync(It.IsAny<RideRequest>())).Returns(Task.CompletedTask);
            _mockDriverRepository.Setup(r => r.GetAvailableDriversAsync()).ReturnsAsync(new List<Driver>());
            _mockOfferRepository.Setup(r => r.AddAsync(It.IsAny<DriverOffer>())).Returns(Task.CompletedTask);

            // Act
            var result = await _service.RequestRideAsync(request);

            // Assert
            Assert.NotNull(result);
            Assert.Equal(request.RiderId, result.RiderId);
            Assert.Equal("requested", result.Status);
            Assert.Equal(50m, result.EstimatedFare);
            _mockRideRepository.Verify(r => r.AddAsync(It.IsAny<RideRequest>()), Times.Once);
        }

        [Fact]
        public async Task RequestRideAsync_WithInvalidLatitude_ShouldThrow()
        {
            // Arrange
            var request = new RequestRideCommand
            {
                RiderId = Guid.NewGuid(),
                PickupLatitude = -91m, // Invalid
                PickupLongitude = -46.6333m,
                DropoffLatitude = -23.5475m,
                DropoffLongitude = -46.6361m
            };

            // Act & Assert
            await Assert.ThrowsAsync<ArgumentException>(() => _service.RequestRideAsync(request));
        }

        [Fact]
        public async Task RequestRideAsync_ShouldSendOffersToNearbyDrivers()
        {
            // Arrange
            var request = new RequestRideCommand
            {
                RiderId = Guid.NewGuid(),
                PickupLatitude = -23.5505m,
                PickupLongitude = -46.6333m,
                DropoffLatitude = -23.5475m,
                DropoffLongitude = -46.6361m,
                EstimatedFare = 50m
            };

            var nearbyDrivers = new List<Driver>();
            for (int i = 0; i < 5; i++)
            {
                var vehicle = new Vehicle("ABC" + i, "Toyota", "White", 2022, 4, "economy", DateTime.UtcNow.AddYears(1));
                var driver = new Driver(
                    Guid.NewGuid(),
                    $"Driver{i}",
                    "Test",
                    $"driver{i}@example.com",
                    "1199999999",
                    $"DL{i}",
                    DateTime.UtcNow.AddYears(2),
                    vehicle);
                nearbyDrivers.Add(driver);
            }

            _mockRideRepository.Setup(r => r.AddAsync(It.IsAny<RideRequest>())).Returns(Task.CompletedTask);
            _mockDriverRepository.Setup(r => r.GetAvailableDriversAsync()).ReturnsAsync(nearbyDrivers);
            _mockOfferRepository.Setup(r => r.AddAsync(It.IsAny<DriverOffer>())).Returns(Task.CompletedTask);

            // Act
            await _service.RequestRideAsync(request);

            // Assert
            _mockOfferRepository.Verify(
                r => r.AddAsync(It.IsAny<DriverOffer>()),
                Times.Exactly(5),
                "Should create offers for 5 nearby drivers");
        }

        [Fact]
        public async Task GetRideAsync_WithExistingRide_ShouldReturnRide()
        {
            // Arrange
            var rideId = Guid.NewGuid();
            var ride = new RideRequest(
                Guid.NewGuid(),
                new Location(-23.5505m, -46.6333m),
                new Location(-23.5475m, -46.6361m),
                "Pickup",
                "Dropoff",
                50m);

            _mockRideRepository.Setup(r => r.GetByIdAsync(rideId)).ReturnsAsync(ride);

            // Act
            var result = await _service.GetRideAsync(rideId);

            // Assert
            Assert.NotNull(result);
            Assert.Equal("requested", result.Status);
        }
    }

    public class TripApplicationServiceTests
    {
        private readonly Mock<ITripRepository> _mockTripRepository;
        private readonly Mock<IRideRequestRepository> _mockRideRepository;
        private readonly Mock<IDriverOfferRepository> _mockOfferRepository;
        private readonly TripApplicationService _service;

        public TripApplicationServiceTests()
        {
            _mockTripRepository = new Mock<ITripRepository>();
            _mockRideRepository = new Mock<IRideRequestRepository>();
            _mockOfferRepository = new Mock<IDriverOfferRepository>();
            _service = new TripApplicationService(
                _mockTripRepository.Object,
                _mockRideRepository.Object,
                _mockOfferRepository.Object);
        }

        [Fact]
        public async Task AcceptOfferAsync_WithValidOffer_ShouldCreateTrip()
        {
            // Arrange
            var rideId = Guid.NewGuid();
            var driverId = Guid.NewGuid();
            var vehicleId = Guid.NewGuid();

            var offer = new DriverOffer(rideId, driverId, TimeSpan.FromSeconds(20));
            var ride = new RideRequest(
                Guid.NewGuid(),
                new Location(-23.5505m, -46.6333m),
                new Location(-23.5475m, -46.6361m),
                "Pickup",
                "Dropoff",
                50m);

            _mockOfferRepository.Setup(r => r.GetByIdAsync(offer.Id)).ReturnsAsync(offer);
            _mockRideRepository.Setup(r => r.GetByIdAsync(rideId)).ReturnsAsync(ride);
            _mockRideRepository.Setup(r => r.UpdateAsync(It.IsAny<RideRequest>())).Returns(Task.CompletedTask);
            _mockOfferRepository.Setup(r => r.UpdateAsync(It.IsAny<DriverOffer>())).Returns(Task.CompletedTask);
            _mockTripRepository.Setup(r => r.AddAsync(It.IsAny<Trip>())).Returns(Task.CompletedTask);

            // Act
            var result = await _service.AcceptOfferAsync(new AcceptOfferCommand
            {
                RideId = rideId,
                OfferId = offer.Id,
                DriverId = driverId,
                VehicleId = vehicleId,
                IdempotencyKey = Guid.NewGuid()
            });

            // Assert
            Assert.NotNull(result);
            Assert.Equal("assigned", result.Status);
            _mockTripRepository.Verify(r => r.AddAsync(It.IsAny<Trip>()), Times.Once);
        }

        [Fact]
        public async Task StartTripAsync_WithValidTrip_ShouldChangeStatus()
        {
            // Arrange
            var tripId = Guid.NewGuid();
            var trip = new Trip(
                Guid.NewGuid(),
                Guid.NewGuid(),
                Guid.NewGuid(),
                Guid.NewGuid(),
                new Location(-23.5505m, -46.6333m),
                new Location(-23.5475m, -46.6361m));

            _mockTripRepository.Setup(r => r.GetByIdAsync(tripId)).ReturnsAsync(trip);
            _mockTripRepository.Setup(r => r.UpdateAsync(It.IsAny<Trip>())).Returns(Task.CompletedTask);

            // Act
            var result = await _service.StartTripAsync(tripId);

            // Assert
            Assert.NotNull(result);
            Assert.Equal("in_progress", result.Status);
            _mockTripRepository.Verify(r => r.UpdateAsync(It.IsAny<Trip>()), Times.Once);
        }

        [Fact]
        public async Task CompleteTripAsync_WithValidData_ShouldCompleteTrip()
        {
            // Arrange
            var tripId = Guid.NewGuid();
            var trip = new Trip(
                Guid.NewGuid(),
                Guid.NewGuid(),
                Guid.NewGuid(),
                Guid.NewGuid(),
                new Location(-23.5505m, -46.6333m),
                new Location(-23.5475m, -46.6361m));

            trip.MarkDriverArrived();
            trip.MarkAsInProgress();

            var ride = new RideRequest(
                trip.RideId,
                new Location(-23.5505m, -46.6333m),
                new Location(-23.5475m, -46.6361m),
                "Pickup",
                "Dropoff",
                50m);

            _mockTripRepository.Setup(r => r.GetByIdAsync(tripId)).ReturnsAsync(trip);
            _mockRideRepository.Setup(r => r.GetByIdAsync(trip.RideId)).ReturnsAsync(ride);
            _mockTripRepository.Setup(r => r.UpdateAsync(It.IsAny<Trip>())).Returns(Task.CompletedTask);
            _mockRideRepository.Setup(r => r.UpdateAsync(It.IsAny<RideRequest>())).Returns(Task.CompletedTask);

            // Act
            var result = await _service.CompleteTripAsync(new CompleteTripCommand
            {
                TripId = tripId,
                DriverId = trip.DriverId,
                FinalFare = 75.50m,
                DistanceTraveled = 5000m,
                DurationSeconds = 600
            });

            // Assert
            Assert.NotNull(result);
            Assert.Equal("completed", result.Status);
            Assert.Equal(75.50m, result.FinalFare);
            _mockTripRepository.Verify(r => r.UpdateAsync(It.IsAny<Trip>()), Times.Once);
        }
    }

    public class ConcurrencyTests
    {
        [Fact]
        public async Task MultipleDriversAcceptingSameOffer_OnlyOneShouldSucceed()
        {
            // Arrange
            var rideId = Guid.NewGuid();
            var riderId = Guid.NewGuid();
            var ride = new RideRequest(
                riderId,
                new Location(-23.5505m, -46.6333m),
                new Location(-23.5475m, -46.6361m),
                "Pickup",
                "Dropoff",
                50m);

            var driver1 = Guid.NewGuid();
            var driver2 = Guid.NewGuid();

            var offer1 = new DriverOffer(rideId, driver1, TimeSpan.FromSeconds(20));
            var offer2 = new DriverOffer(rideId, driver2, TimeSpan.FromSeconds(20));

            // Simulate two parallel acceptance attempts
            var acceptanceAttempts = new List<Task>();

            // This is a simplified test; in real implementation would use actual concurrency
            // Test verifies that ride can't be double-assigned (optimistic locking concept)
            ride.AssignDriver(driver1);
            
            // Second attempt should fail
            Assert.Throws<InvalidOperationException>(() => ride.AssignDriver(driver2));
        }

        [Fact]
        public async Task RideCancellationDuringMatching_ShouldPreventTrip()
        {
            // Arrange
            var ride = new RideRequest(
                Guid.NewGuid(),
                new Location(-23.5505m, -46.6333m),
                new Location(-23.5475m, -46.6361m),
                "Pickup",
                "Dropoff",
                50m);

            // Rider cancels before driver accepts
            ride.Cancel();

            // Act & Assert
            Assert.Throws<InvalidOperationException>(() => 
                ride.AssignDriver(Guid.NewGuid()));
        }
    }
}
