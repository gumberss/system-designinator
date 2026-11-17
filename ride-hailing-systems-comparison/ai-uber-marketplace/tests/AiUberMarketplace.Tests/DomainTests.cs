using System;
using System.Collections.Generic;
using System.Linq;
using Xunit;
using AiUberMarketplace.Domain;

namespace AiUberMarketplace.Tests.Domain
{
    public class RiderAggregateTests
    {
        [Fact]
        public void CreateRider_WithValidData_ShouldRaiseRiderRegisteredEvent()
        {
            // Arrange
            var userId = Guid.NewGuid();
            var firstName = "João";
            var lastName = "Silva";
            var email = "joao@example.com";
            var phoneNumber = "11999999999";

            // Act
            var rider = new Rider(userId, firstName, lastName, email, phoneNumber);

            // Assert
            Assert.NotEqual(Guid.Empty, rider.Id);
            Assert.Equal(userId, rider.UserId);
            Assert.Equal(firstName, rider.FirstName);
            Assert.Equal(lastName, rider.LastName);
            Assert.Equal(email, rider.Email);
            Assert.Equal(phoneNumber, rider.PhoneNumber);
            Assert.Equal("regular", rider.RiderType);

            var events = rider.GetUncommittedEvents();
            Assert.Single(events);
            Assert.IsType<RiderRegisteredEvent>(events[0]);
        }

        [Fact]
        public void CreateRider_WithoutEmail_ShouldThrow()
        {
            // Arrange
            var userId = Guid.NewGuid();

            // Act & Assert
            Assert.Throws<ArgumentNullException>(() =>
                new Rider(userId, "João", "Silva", null, "11999999999"));
        }

        [Fact]
        public void CreateRider_WithoutFirstName_ShouldThrow()
        {
            // Act & Assert
            Assert.Throws<ArgumentNullException>(() =>
                new Rider(Guid.NewGuid(), null, "Silva", "joao@example.com", "11999999999"));
        }

        [Fact]
        public void CreateRider_WithPremiumType_ShouldSetCorrectly()
        {
            // Arrange
            var userId = Guid.NewGuid();

            // Act
            var rider = new Rider(
                userId,
                "João",
                "Silva",
                "joao@example.com",
                "11999999999",
                "premium");

            // Assert
            Assert.Equal("premium", rider.RiderType);
        }
    }

    public class DriverAggregateTests
    {
        [Fact]
        public void CreateDriver_WithValidData_ShouldRaiseDriverRegisteredEvent()
        {
            // Arrange
            var userId = Guid.NewGuid();
            var vehicle = new Vehicle(
                "ABC1234",
                "Toyota Corolla",
                "White",
                2022,
                4,
                "economy",
                DateTime.UtcNow.AddYears(1));

            // Act
            var driver = new Driver(
                userId,
                "Maria",
                "Santos",
                "maria@example.com",
                "11988888888",
                "DL123456",
                DateTime.UtcNow.AddYears(2),
                vehicle);

            // Assert
            Assert.NotEqual(Guid.Empty, driver.Id);
            Assert.Equal(userId, driver.UserId);
            Assert.Equal("Maria", driver.FirstName);
            Assert.False(driver.IsAvailable);
            Assert.Equal(0, driver.AverageRating);
            Assert.Equal(0, driver.TotalTrips);

            var events = driver.GetUncommittedEvents();
            Assert.Single(events);
            Assert.IsType<DriverRegisteredEvent>(events[0]);
        }

        [Fact]
        public void GoOnline_WithValidLocation_ShouldMarkAvailable()
        {
            // Arrange
            var vehicle = CreateValidVehicle();
            var driver = CreateValidDriver(vehicle);
            var location = new Location(-23.5505m, -46.6333m); // São Paulo

            // Act
            driver.GoOnline(location);

            // Assert
            Assert.True(driver.IsAvailable);
            Assert.NotNull(driver.CurrentLocation);
            Assert.Equal(-23.5505m, driver.CurrentLocation.Latitude);
            Assert.Equal(-46.6333m, driver.CurrentLocation.Longitude);

            var events = driver.GetUncommittedEvents().ToList();
            Assert.Equal(2, events.Count);
            Assert.IsType<DriverAvailabilityChangedEvent>(events[1]);
        }

        [Fact]
        public void GoOnline_WithExpiredLicense_ShouldThrow()
        {
            // Arrange
            var vehicle = CreateValidVehicle();
            var driver = new Driver(
                Guid.NewGuid(),
                "Maria",
                "Santos",
                "maria@example.com",
                "11988888888",
                "DL123456",
                DateTime.UtcNow.AddDays(-1), // Expired
                vehicle);

            var location = new Location(-23.5505m, -46.6333m);

            // Act & Assert
            Assert.Throws<InvalidOperationException>(() => driver.GoOnline(location));
        }

        [Fact]
        public void GoOffline_ShouldMarkUnavailable()
        {
            // Arrange
            var vehicle = CreateValidVehicle();
            var driver = CreateValidDriver(vehicle);
            var location = new Location(-23.5505m, -46.6333m);
            driver.GoOnline(location);

            // Act
            driver.GoOffline();

            // Assert
            Assert.False(driver.IsAvailable);
        }

        [Fact]
        public void UpdateLocation_ShouldChangeCurrentLocation()
        {
            // Arrange
            var vehicle = CreateValidVehicle();
            var driver = CreateValidDriver(vehicle);
            var initialLocation = new Location(-23.5505m, -46.6333m);
            driver.GoOnline(initialLocation);

            var newLocation = new Location(-23.5475m, -46.6361m);

            // Act
            driver.UpdateLocation(newLocation);

            // Assert
            Assert.Equal(-23.5475m, driver.CurrentLocation.Latitude);
            Assert.Equal(-46.6361m, driver.CurrentLocation.Longitude);
        }

        private Vehicle CreateValidVehicle()
        {
            return new Vehicle(
                "ABC1234",
                "Toyota Corolla",
                "White",
                2022,
                4,
                "economy",
                DateTime.UtcNow.AddYears(1));
        }

        private Driver CreateValidDriver(Vehicle vehicle)
        {
            return new Driver(
                Guid.NewGuid(),
                "Maria",
                "Santos",
                "maria@example.com",
                "11988888888",
                "DL123456",
                DateTime.UtcNow.AddYears(2),
                vehicle);
        }
    }

    public class RideRequestAggregateTests
    {
        [Fact]
        public void RequestRide_WithValidData_ShouldRaiseRideRequestedEvent()
        {
            // Arrange
            var riderId = Guid.NewGuid();
            var pickupLocation = new Location(-23.5505m, -46.6333m);
            var dropoffLocation = new Location(-23.5475m, -46.6361m);

            // Act
            var rideRequest = new RideRequest(
                riderId,
                pickupLocation,
                dropoffLocation,
                "Av Paulista, 100",
                "Rua Augusta, 200",
                50.00m);

            // Assert
            Assert.NotEqual(Guid.Empty, rideRequest.Id);
            Assert.Equal(riderId, rideRequest.RiderId);
            Assert.Equal("requested", rideRequest.Status);
            Assert.Equal(50.00m, rideRequest.EstimatedFare);
            Assert.Null(rideRequest.AssignedDriverId);

            var events = rideRequest.GetUncommittedEvents();
            Assert.Single(events);
            Assert.IsType<RideRequestedEvent>(events[0]);
        }

        [Fact]
        public void AssignDriver_WithValidDriverId_ShouldChangeStatus()
        {
            // Arrange
            var rideRequest = CreateValidRideRequest();
            var driverId = Guid.NewGuid();

            // Act
            rideRequest.AssignDriver(driverId);

            // Assert
            Assert.Equal("assigned", rideRequest.Status);
            Assert.Equal(driverId, rideRequest.AssignedDriverId);

            var events = rideRequest.GetUncommittedEvents().ToList();
            Assert.Equal(2, events.Count);
            Assert.IsType<RideAssignedEvent>(events[1]);
        }

        [Fact]
        public void AssignDriver_ToAlreadyAssignedRide_ShouldThrow()
        {
            // Arrange
            var rideRequest = CreateValidRideRequest();
            var driverId1 = Guid.NewGuid();
            var driverId2 = Guid.NewGuid();

            rideRequest.AssignDriver(driverId1);

            // Act & Assert
            Assert.Throws<InvalidOperationException>(() =>
                rideRequest.AssignDriver(driverId2));
        }

        [Fact]
        public void Cancel_ShouldMarkCancelled()
        {
            // Arrange
            var rideRequest = CreateValidRideRequest();

            // Act
            rideRequest.Cancel();

            // Assert
            Assert.Equal("cancelled", rideRequest.Status);
            Assert.NotNull(rideRequest.CancelledAt);
        }

        private RideRequest CreateValidRideRequest()
        {
            return new RideRequest(
                Guid.NewGuid(),
                new Location(-23.5505m, -46.6333m),
                new Location(-23.5475m, -46.6361m),
                "Av Paulista, 100",
                "Rua Augusta, 200",
                50.00m);
        }
    }

    public class TripAggregateTests
    {
        [Fact]
        public void CreateTrip_WithValidData_ShouldRaiseTripCreatedEvent()
        {
            // Arrange
            var rideId = Guid.NewGuid();
            var driverId = Guid.NewGuid();
            var riderId = Guid.NewGuid();
            var vehicleId = Guid.NewGuid();
            var pickupLocation = new Location(-23.5505m, -46.6333m);
            var dropoffLocation = new Location(-23.5475m, -46.6361m);

            // Act
            var trip = new Trip(rideId, driverId, riderId, vehicleId, pickupLocation, dropoffLocation);

            // Assert
            Assert.NotEqual(Guid.Empty, trip.Id);
            Assert.Equal("assigned", trip.Status);
            Assert.Equal(driverId, trip.DriverId);
            Assert.Equal(riderId, trip.RiderId);

            var events = trip.GetUncommittedEvents();
            Assert.Single(events);
            Assert.IsType<TripCreatedEvent>(events[0]);
        }

        [Fact]
        public void MarkDriverArrived_ShouldChangeStatus()
        {
            // Arrange
            var trip = CreateValidTrip();

            // Act
            trip.MarkDriverArrived();

            // Assert
            Assert.Equal("arrived", trip.Status);
        }

        [Fact]
        public void MarkAsInProgress_FromArrivedStatus_ShouldSetStartTime()
        {
            // Arrange
            var trip = CreateValidTrip();
            trip.MarkDriverArrived();

            // Act
            trip.MarkAsInProgress();

            // Assert
            Assert.Equal("in_progress", trip.Status);
            Assert.NotNull(trip.StartedAt);
        }

        [Fact]
        public void MarkAsInProgress_FromNonArrivedStatus_ShouldThrow()
        {
            // Arrange
            var trip = CreateValidTrip();

            // Act & Assert
            Assert.Throws<InvalidOperationException>(() => trip.MarkAsInProgress());
        }

        [Fact]
        public void Complete_WithValidData_ShouldUpdateTripState()
        {
            // Arrange
            var trip = CreateValidTrip();
            trip.MarkDriverArrived();
            trip.MarkAsInProgress();

            var fare = 75.50m;
            var distance = 5000m;
            var duration = 600;

            // Act
            trip.Complete(fare, distance, duration);

            // Assert
            Assert.Equal("completed", trip.Status);
            Assert.Equal(fare, trip.FinalFare);
            Assert.Equal(distance, trip.DistanceTraveledMeters);
            Assert.Equal(duration, trip.DurationSeconds);
            Assert.NotNull(trip.CompletedAt);
        }

        [Fact]
        public void Complete_FromNonInProgressStatus_ShouldThrow()
        {
            // Arrange
            var trip = CreateValidTrip();

            // Act & Assert
            Assert.Throws<InvalidOperationException>(() =>
                trip.Complete(50m, 1000m, 300));
        }

        private Trip CreateValidTrip()
        {
            return new Trip(
                Guid.NewGuid(),
                Guid.NewGuid(),
                Guid.NewGuid(),
                Guid.NewGuid(),
                new Location(-23.5505m, -46.6333m),
                new Location(-23.5475m, -46.6361m));
        }
    }

    public class DriverOfferAggregateTests
    {
        [Fact]
        public void CreateOffer_WithValidData_ShouldRaiseDriverOfferSentEvent()
        {
            // Arrange
            var rideId = Guid.NewGuid();
            var driverId = Guid.NewGuid();
            var offerDuration = TimeSpan.FromSeconds(20);

            // Act
            var offer = new DriverOffer(rideId, driverId, offerDuration);

            // Assert
            Assert.NotEqual(Guid.Empty, offer.Id);
            Assert.Equal("sent", offer.Status);
            Assert.Equal(rideId, offer.RideId);
            Assert.Equal(driverId, offer.DriverId);

            var events = offer.GetUncommittedEvents();
            Assert.Single(events);
            Assert.IsType<DriverOfferSentEvent>(events[0]);
        }

        [Fact]
        public void Accept_WithinTimeLimit_ShouldChangeStatus()
        {
            // Arrange
            var offer = new DriverOffer(Guid.NewGuid(), Guid.NewGuid(), TimeSpan.FromSeconds(20));

            // Act
            offer.Accept();

            // Assert
            Assert.Equal("accepted", offer.Status);
            Assert.NotNull(offer.RespondedAt);
        }

        [Fact]
        public void Accept_AfterExpiration_ShouldThrow()
        {
            // Arrange
            var offer = new DriverOffer(Guid.NewGuid(), Guid.NewGuid(), TimeSpan.FromMilliseconds(1));
            System.Threading.Thread.Sleep(100); // Wait for expiration

            // Act & Assert
            Assert.Throws<InvalidOperationException>(() => offer.Accept());
        }

        [Fact]
        public void Reject_ShouldChangeStatus()
        {
            // Arrange
            var offer = new DriverOffer(Guid.NewGuid(), Guid.NewGuid(), TimeSpan.FromSeconds(20));

            // Act
            offer.Reject();

            // Assert
            Assert.Equal("rejected", offer.Status);
            Assert.NotNull(offer.RespondedAt);
        }

        [Fact]
        public void IsExpired_BeforeExpiration_ShouldReturnFalse()
        {
            // Arrange
            var offer = new DriverOffer(Guid.NewGuid(), Guid.NewGuid(), TimeSpan.FromSeconds(20));

            // Act
            var isExpired = offer.IsExpired();

            // Assert
            Assert.False(isExpired);
        }
    }

    public class LocationValueObjectTests
    {
        [Fact]
        public void CreateLocation_WithValidCoordinates_ShouldSucceed()
        {
            // Act
            var location = new Location(-23.5505m, -46.6333m);

            // Assert
            Assert.Equal(-23.5505m, location.Latitude);
            Assert.Equal(-46.6333m, location.Longitude);
        }

        [Theory]
        [InlineData(-91, 0)]
        [InlineData(91, 0)]
        [InlineData(0, -181)]
        [InlineData(0, 181)]
        public void CreateLocation_WithInvalidCoordinates_ShouldThrow(decimal lat, decimal lon)
        {
            // Act & Assert
            Assert.Throws<InvalidOperationException>(() => new Location(lat, lon));
        }

        [Fact]
        public void LocationEquality_WithSameCoordinates_ShouldBeEqual()
        {
            // Arrange
            var location1 = new Location(-23.5505m, -46.6333m);
            var location2 = new Location(-23.5505m, -46.6333m);

            // Assert
            Assert.Equal(location1, location2);
        }

        [Fact]
        public void LocationEquality_WithDifferentCoordinates_ShouldNotBeEqual()
        {
            // Arrange
            var location1 = new Location(-23.5505m, -46.6333m);
            var location2 = new Location(-23.5475m, -46.6361m);

            // Assert
            Assert.NotEqual(location1, location2);
        }
    }

    public class VehicleValueObjectTests
    {
        [Fact]
        public void CreateVehicle_WithValidData_ShouldSucceed()
        {
            // Act
            var vehicle = new Vehicle(
                "ABC1234",
                "Toyota Corolla",
                "White",
                2022,
                4,
                "economy",
                DateTime.UtcNow.AddYears(1));

            // Assert
            Assert.NotEqual(Guid.Empty, vehicle.VehicleId);
            Assert.Equal("ABC1234", vehicle.LicensePlate);
            Assert.Equal("Toyota Corolla", vehicle.Model);
            Assert.Equal(2022, vehicle.Year);
            Assert.True(vehicle.IsActive);
        }

        [Fact]
        public void IsInsuranceValid_WithFutureExpiry_ShouldReturnTrue()
        {
            // Arrange
            var vehicle = new Vehicle(
                "ABC1234",
                "Toyota Corolla",
                "White",
                2022,
                4,
                "economy",
                DateTime.UtcNow.AddYears(1));

            // Act & Assert
            Assert.True(vehicle.IsInsuranceValid());
        }

        [Fact]
        public void IsInsuranceValid_WithPastExpiry_ShouldReturnFalse()
        {
            // Arrange
            var vehicle = new Vehicle(
                "ABC1234",
                "Toyota Corolla",
                "White",
                2022,
                4,
                "economy",
                DateTime.UtcNow.AddDays(-1));

            // Act & Assert
            Assert.False(vehicle.IsInsuranceValid());
        }
    }

    public class AggregateRootTests
    {
        [Fact]
        public void GetUncommittedEvents_ShouldReturnAllRaisedEvents()
        {
            // Arrange
            var rider = new Rider(
                Guid.NewGuid(),
                "João",
                "Silva",
                "joao@example.com",
                "11999999999");

            // Act
            var events = rider.GetUncommittedEvents();

            // Assert
            Assert.NotEmpty(events);
            Assert.All(events, e => Assert.NotNull(e.EventId));
            Assert.All(events, e => Assert.NotNull(e.AggregateId));
        }

        [Fact]
        public void ClearUncommittedEvents_ShouldRemoveAllEvents()
        {
            // Arrange
            var rider = new Rider(
                Guid.NewGuid(),
                "João",
                "Silva",
                "joao@example.com",
                "11999999999");

            // Act
            rider.ClearUncommittedEvents();

            // Assert
            Assert.Empty(rider.GetUncommittedEvents());
        }

        [Fact]
        public void Version_ShouldIncrementWhenEventsRaised()
        {
            // Arrange
            var rideRequest = new RideRequest(
                Guid.NewGuid(),
                new Location(-23.5505m, -46.6333m),
                new Location(-23.5475m, -46.6361m),
                "Pickup",
                "Dropoff",
                50m);

            var initialVersion = rideRequest.Version;

            // Act
            rideRequest.AssignDriver(Guid.NewGuid());

            // Assert
            Assert.Equal(initialVersion + 1, rideRequest.Version);
        }
    }
}
