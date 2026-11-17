using AiUberMarketplace.Domain;
using Microsoft.EntityFrameworkCore;
using Microsoft.EntityFrameworkCore.Storage.ValueConversion;

namespace AiUberMarketplace.Infrastructure.Persistence
{
    /// <summary>
    /// EF Core DbContext for AI Uber Marketplace
    /// Manages all aggregate roots and entities
    /// </summary>
    public class AiUberMarketplaceDbContext : DbContext
    {
        public AiUberMarketplaceDbContext(DbContextOptions<AiUberMarketplaceDbContext> options)
            : base(options)
        {
        }

        // Aggregates
        public DbSet<Rider> Riders { get; set; }
        public DbSet<Driver> Drivers { get; set; }
        public DbSet<Trip> Trips { get; set; }
        public DbSet<RideRequest> RideRequests { get; set; }
        public DbSet<Payment> Payments { get; set; }
        public DbSet<Offer> Offers { get; set; }
        public DbSet<LocationHistory> LocationHistories { get; set; }
        public DbSet<OutboxEvent> OutboxEvents { get; set; }

        protected override void OnModelCreating(ModelBuilder modelBuilder)
        {
            base.OnModelCreating(modelBuilder);

            // ===== Rider Configuration =====
            modelBuilder.Entity<Rider>(b =>
            {
                b.ToTable("riders");
                b.HasKey(r => r.Id);
                b.Property(r => r.Id).HasColumnName("id");
                b.Property(r => r.RiderId).HasColumnName("rider_id");
                b.Property(r => r.UserId).HasColumnName("user_id");
                b.Property(r => r.FirstName).HasColumnName("first_name").IsRequired();
                b.Property(r => r.LastName).HasColumnName("last_name").IsRequired();
                b.Property(r => r.Email).HasColumnName("email").IsRequired();
                b.Property(r => r.PhoneNumber).HasColumnName("phone_number");
                b.Property(r => r.RiderType).HasColumnName("rider_type").IsRequired().HasDefaultValue("regular");
                b.Property(r => r.CreatedAt).HasColumnName("created_at").IsRequired();
                b.Property(r => r.UpdatedAt).HasColumnName("updated_at").IsRequired();
                b.Property(r => r.Version).HasColumnName("version").IsConcurrencyToken();

                b.HasIndex(r => r.Email).IsUnique();
                b.HasIndex(r => r.UserId);
            });

            // ===== Driver Configuration =====
            modelBuilder.Entity<Driver>(b =>
            {
                b.ToTable("drivers");
                b.HasKey(d => d.Id);
                b.Property(d => d.Id).HasColumnName("id");
                b.Property(d => d.DriverId).HasColumnName("driver_id");
                b.Property(d => d.UserId).HasColumnName("user_id");
                b.Property(d => d.FirstName).HasColumnName("first_name").IsRequired();
                b.Property(d => d.LastName).HasColumnName("last_name").IsRequired();
                b.Property(d => d.Email).HasColumnName("email").IsRequired();
                b.Property(d => d.PhoneNumber).HasColumnName("phone_number");
                b.Property(d => d.LicenseNumber).HasColumnName("license_number").IsRequired();
                b.Property(d => d.LicenseExpiry).HasColumnName("license_expiry").IsRequired();
                b.Property(d => d.IsAvailable).HasColumnName("is_available").IsRequired().HasDefaultValue(false);
                b.Property(d => d.AverageRating).HasColumnName("average_rating").IsRequired().HasDefaultValue(0m);
                b.Property(d => d.TotalTrips).HasColumnName("total_trips").IsRequired().HasDefaultValue(0);
                b.Property(d => d.CreatedAt).HasColumnName("created_at").IsRequired();
                b.Property(d => d.UpdatedAt).HasColumnName("updated_at").IsRequired();
                b.Property(d => d.Version).HasColumnName("version").IsConcurrencyToken();

                // Location value object
                b.OwnsOne(d => d.CurrentLocation, loc =>
                {
                    loc.Property(l => l.Latitude).HasColumnName("current_latitude");
                    loc.Property(l => l.Longitude).HasColumnName("current_longitude");
                });

                // Vehicle value object
                b.OwnsOne(d => d.Vehicle, v =>
                {
                    v.Property(ve => ve.VehicleId).HasColumnName("vehicle_id");
                    v.Property(ve => ve.LicensePlate).HasColumnName("license_plate").IsRequired();
                    v.Property(ve => ve.Model).HasColumnName("vehicle_model").IsRequired();
                    v.Property(ve => ve.Color).HasColumnName("vehicle_color");
                    v.Property(ve => ve.Year).HasColumnName("vehicle_year");
                    v.Property(ve => ve.Seats).HasColumnName("vehicle_seats");
                    v.Property(ve => ve.VehicleType).HasColumnName("vehicle_type");
                    v.Property(ve => ve.InsuranceExpiry).HasColumnName("insurance_expiry");
                    v.Property(ve => ve.IsActive).HasColumnName("vehicle_is_active");
                });

                b.HasIndex(d => d.Email).IsUnique();
                b.HasIndex(d => d.LicenseNumber).IsUnique();
                b.HasIndex(d => d.IsAvailable);
            });

            // ===== Trip Configuration =====
            modelBuilder.Entity<Trip>(b =>
            {
                b.ToTable("trips");
                b.HasKey(t => t.Id);
                b.Property(t => t.Id).HasColumnName("id");
                b.Property(t => t.TripId).HasColumnName("trip_id");
                b.Property(t => t.RideId).HasColumnName("ride_id");
                b.Property(t => t.DriverId).HasColumnName("driver_id");
                b.Property(t => t.RiderId).HasColumnName("rider_id");
                b.Property(t => t.VehicleId).HasColumnName("vehicle_id");
                b.Property(t => t.Status).HasColumnName("status").IsRequired();
                b.Property(t => t.StartedAt).HasColumnName("started_at");
                b.Property(t => t.CompletedAt).HasColumnName("completed_at");
                b.Property(t => t.DistanceTraveledMeters).HasColumnName("distance_traveled_meters");
                b.Property(t => t.DurationSeconds).HasColumnName("duration_seconds");
                b.Property(t => t.FinalFare).HasColumnName("final_fare");
                b.Property(t => t.CreatedAt).HasColumnName("created_at").IsRequired();
                b.Property(t => t.UpdatedAt).HasColumnName("updated_at").IsRequired();
                b.Property(t => t.RowVersion).HasColumnName("row_version").IsRowVersion();
                b.Property(t => t.Version).HasColumnName("version");

                // Pickup location
                b.OwnsOne(t => t.PickupLocation, loc =>
                {
                    loc.Property(l => l.Latitude).HasColumnName("pickup_latitude").IsRequired();
                    loc.Property(l => l.Longitude).HasColumnName("pickup_longitude").IsRequired();
                });

                // Dropoff location
                b.OwnsOne(t => t.DropoffLocation, loc =>
                {
                    loc.Property(l => l.Latitude).HasColumnName("dropoff_latitude").IsRequired();
                    loc.Property(l => l.Longitude).HasColumnName("dropoff_longitude").IsRequired();
                });

                // Current location
                b.OwnsOne(t => t.CurrentLocation, loc =>
                {
                    loc.Property(l => l.Latitude).HasColumnName("current_latitude");
                    loc.Property(l => l.Longitude).HasColumnName("current_longitude");
                });

                b.HasOne<Driver>()
                    .WithMany()
                    .HasForeignKey(t => t.DriverId)
                    .OnDelete(DeleteBehavior.Restrict);

                b.HasOne<Rider>()
                    .WithMany()
                    .HasForeignKey(t => t.RiderId)
                    .OnDelete(DeleteBehavior.Restrict);

                b.HasIndex(t => t.Status);
                b.HasIndex(t => t.DriverId);
                b.HasIndex(t => t.RiderId);
                b.HasIndex(t => t.CreatedAt);
            });

            // ===== RideRequest Configuration =====
            modelBuilder.Entity<RideRequest>(b =>
            {
                b.ToTable("ride_requests");
                b.HasKey(r => r.Id);
                b.Property(r => r.Id).HasColumnName("id");
                b.Property(r => r.RideId).HasColumnName("ride_id");
                b.Property(r => r.RiderId).HasColumnName("rider_id");
                b.Property(r => r.PickupAddress).HasColumnName("pickup_address");
                b.Property(r => r.DropoffAddress).HasColumnName("dropoff_address");
                b.Property(r => r.Status).HasColumnName("status").IsRequired();
                b.Property(r => r.EstimatedFare).HasColumnName("estimated_fare");
                b.Property(r => r.AssignedDriverId).HasColumnName("assigned_driver_id");
                b.Property(r => r.CreatedAt).HasColumnName("created_at").IsRequired();
                b.Property(r => r.UpdatedAt).HasColumnName("updated_at").IsRequired();
                b.Property(r => r.CancelledAt).HasColumnName("cancelled_at");
                b.Property(r => r.RowVersion).HasColumnName("row_version").IsRowVersion();
                b.Property(r => r.Version).HasColumnName("version");

                // Pickup location
                b.OwnsOne(r => r.PickupLocation, loc =>
                {
                    loc.Property(l => l.Latitude).HasColumnName("pickup_latitude").IsRequired();
                    loc.Property(l => l.Longitude).HasColumnName("pickup_longitude").IsRequired();
                });

                // Dropoff location
                b.OwnsOne(r => r.DropoffLocation, loc =>
                {
                    loc.Property(l => l.Latitude).HasColumnName("dropoff_latitude").IsRequired();
                    loc.Property(l => l.Longitude).HasColumnName("dropoff_longitude").IsRequired();
                });

                b.HasOne<Rider>()
                    .WithMany()
                    .HasForeignKey(r => r.RiderId)
                    .OnDelete(DeleteBehavior.Cascade);

                b.HasIndex(r => r.Status);
                b.HasIndex(r => r.RiderId);
                b.HasIndex(r => r.CreatedAt);
            });

            // ===== Payment Configuration =====
            modelBuilder.Entity<Payment>(b =>
            {
                b.ToTable("payments");
                b.HasKey(p => p.Id);
                b.Property(p => p.Id).HasColumnName("id");
                b.Property(p => p.PaymentId).HasColumnName("payment_id");
                b.Property(p => p.TripId).HasColumnName("trip_id");
                b.Property(p => p.Amount).HasColumnName("amount").IsRequired();
                b.Property(p => p.PaymentMethod).HasColumnName("payment_method").IsRequired();
                b.Property(p => p.Status).HasColumnName("status").IsRequired();
                b.Property(p => p.TransactionId).HasColumnName("transaction_id");
                b.Property(p => p.CreatedAt).HasColumnName("created_at").IsRequired();
                b.Property(p => p.UpdatedAt).HasColumnName("updated_at").IsRequired();

                b.HasOne<Trip>()
                    .WithMany()
                    .HasForeignKey(p => p.TripId)
                    .OnDelete(DeleteBehavior.Cascade);

                b.HasIndex(p => p.TripId).IsUnique();
                b.HasIndex(p => p.Status);
                b.HasIndex(p => p.TransactionId).IsUnique();
            });

            // ===== Offer Configuration =====
            modelBuilder.Entity<Offer>(b =>
            {
                b.ToTable("offers");
                b.HasKey(o => o.Id);
                b.Property(o => o.Id).HasColumnName("id");
                b.Property(o => o.OfferId).HasColumnName("offer_id");
                b.Property(o => o.TripId).HasColumnName("trip_id");
                b.Property(o => o.DriverId).HasColumnName("driver_id");
                b.Property(o => o.ExpiresAt).HasColumnName("expires_at").IsRequired();
                b.Property(o => o.Status).HasColumnName("status").IsRequired();
                b.Property(o => o.CreatedAt).HasColumnName("created_at").IsRequired();
                b.Property(o => o.UpdatedAt).HasColumnName("updated_at");

                b.HasOne<Trip>()
                    .WithMany()
                    .HasForeignKey(o => o.TripId)
                    .OnDelete(DeleteBehavior.Cascade);

                b.HasOne<Driver>()
                    .WithMany()
                    .HasForeignKey(o => o.DriverId)
                    .OnDelete(DeleteBehavior.Cascade);

                b.HasIndex(o => o.Status);
                b.HasIndex(o => o.DriverId);
                b.HasIndex(o => o.ExpiresAt);
                b.HasIndex(o => new { o.TripId, o.DriverId }).IsUnique();
            });

            // ===== LocationHistory Configuration =====
            modelBuilder.Entity<LocationHistory>(b =>
            {
                b.ToTable("location_history");
                b.HasKey(l => l.Id);
                b.Property(l => l.Id).HasColumnName("id");
                b.Property(l => l.DriverId).HasColumnName("driver_id").IsRequired();
                b.Property(l => l.Latitude).HasColumnName("latitude").IsRequired();
                b.Property(l => l.Longitude).HasColumnName("longitude").IsRequired();
                b.Property(l => l.Heading).HasColumnName("heading");
                b.Property(l => l.Speed).HasColumnName("speed");
                b.Property(l => l.Timestamp).HasColumnName("timestamp").IsRequired();

                b.HasOne<Driver>()
                    .WithMany()
                    .HasForeignKey(l => l.DriverId)
                    .OnDelete(DeleteBehavior.Cascade);

                b.HasIndex(l => new { l.DriverId, l.Timestamp });
                b.HasIndex(l => l.Timestamp);
            });

            // ===== OutboxEvent Configuration (for transactional outbox pattern) =====
            modelBuilder.Entity<OutboxEvent>(b =>
            {
                b.ToTable("outbox_events");
                b.HasKey(o => o.Id);
                b.Property(o => o.Id).HasColumnName("id");
                b.Property(o => o.EventType).HasColumnName("event_type").IsRequired();
                b.Property(o => o.Payload).HasColumnName("payload").IsRequired();
                b.Property(o => o.CreatedAt).HasColumnName("created_at").IsRequired();
                b.Property(o => o.ProcessedAt).HasColumnName("processed_at");

                b.HasIndex(o => o.ProcessedAt);
                b.HasIndex(o => o.CreatedAt);
            });
        }
    }
}
