using System;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace AiUberMarketplace.Infrastructure.Migrations
{
    /// <inheritdoc />
    public partial class InitialCreate : Migration
    {
        /// <inheritdoc />
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.CreateTable(
                name: "drivers",
                columns: table => new
                {
                    id = table.Column<Guid>(type: "uuid", nullable: false),
                    driver_id = table.Column<Guid>(type: "uuid", nullable: false),
                    user_id = table.Column<Guid>(type: "uuid", nullable: false),
                    first_name = table.Column<string>(type: "text", nullable: false),
                    last_name = table.Column<string>(type: "text", nullable: false),
                    email = table.Column<string>(type: "text", nullable: false),
                    phone_number = table.Column<string>(type: "text", nullable: false),
                    license_number = table.Column<string>(type: "text", nullable: false),
                    license_expiry = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    is_available = table.Column<bool>(type: "boolean", nullable: false, defaultValue: false),
                    current_latitude = table.Column<decimal>(type: "numeric", nullable: false),
                    current_longitude = table.Column<decimal>(type: "numeric", nullable: false),
                    vehicle_id = table.Column<Guid>(type: "uuid", nullable: false),
                    license_plate = table.Column<string>(type: "text", nullable: false),
                    vehicle_model = table.Column<string>(type: "text", nullable: false),
                    vehicle_color = table.Column<string>(type: "text", nullable: false),
                    vehicle_year = table.Column<int>(type: "integer", nullable: false),
                    vehicle_seats = table.Column<int>(type: "integer", nullable: false),
                    vehicle_type = table.Column<string>(type: "text", nullable: false),
                    insurance_expiry = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    vehicle_is_active = table.Column<bool>(type: "boolean", nullable: false),
                    average_rating = table.Column<decimal>(type: "numeric", nullable: false, defaultValue: 0m),
                    total_trips = table.Column<int>(type: "integer", nullable: false, defaultValue: 0),
                    created_at = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    updated_at = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    version = table.Column<int>(type: "integer", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_drivers", x => x.id);
                });

            migrationBuilder.CreateTable(
                name: "outbox_events",
                columns: table => new
                {
                    id = table.Column<Guid>(type: "uuid", nullable: false),
                    event_type = table.Column<string>(type: "text", nullable: false),
                    payload = table.Column<string>(type: "text", nullable: false),
                    created_at = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    processed_at = table.Column<DateTime>(type: "timestamp with time zone", nullable: true)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_outbox_events", x => x.id);
                });

            migrationBuilder.CreateTable(
                name: "riders",
                columns: table => new
                {
                    id = table.Column<Guid>(type: "uuid", nullable: false),
                    rider_id = table.Column<Guid>(type: "uuid", nullable: false),
                    user_id = table.Column<Guid>(type: "uuid", nullable: false),
                    first_name = table.Column<string>(type: "text", nullable: false),
                    last_name = table.Column<string>(type: "text", nullable: false),
                    email = table.Column<string>(type: "text", nullable: false),
                    phone_number = table.Column<string>(type: "text", nullable: false),
                    rider_type = table.Column<string>(type: "text", nullable: false, defaultValue: "regular"),
                    created_at = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    updated_at = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    version = table.Column<int>(type: "integer", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_riders", x => x.id);
                });

            migrationBuilder.CreateTable(
                name: "location_history",
                columns: table => new
                {
                    id = table.Column<Guid>(type: "uuid", nullable: false),
                    driver_id = table.Column<Guid>(type: "uuid", nullable: false),
                    latitude = table.Column<decimal>(type: "numeric", nullable: false),
                    longitude = table.Column<decimal>(type: "numeric", nullable: false),
                    heading = table.Column<decimal>(type: "numeric", nullable: true),
                    speed = table.Column<decimal>(type: "numeric", nullable: true),
                    timestamp = table.Column<DateTime>(type: "timestamp with time zone", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_location_history", x => x.id);
                    table.ForeignKey(
                        name: "FK_location_history_drivers_driver_id",
                        column: x => x.driver_id,
                        principalTable: "drivers",
                        principalColumn: "id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateTable(
                name: "ride_requests",
                columns: table => new
                {
                    id = table.Column<Guid>(type: "uuid", nullable: false),
                    ride_id = table.Column<Guid>(type: "uuid", nullable: false),
                    rider_id = table.Column<Guid>(type: "uuid", nullable: false),
                    pickup_latitude = table.Column<decimal>(type: "numeric", nullable: false),
                    pickup_longitude = table.Column<decimal>(type: "numeric", nullable: false),
                    dropoff_latitude = table.Column<decimal>(type: "numeric", nullable: false),
                    dropoff_longitude = table.Column<decimal>(type: "numeric", nullable: false),
                    pickup_address = table.Column<string>(type: "text", nullable: false),
                    dropoff_address = table.Column<string>(type: "text", nullable: false),
                    status = table.Column<string>(type: "text", nullable: false),
                    estimated_fare = table.Column<decimal>(type: "numeric", nullable: false),
                    assigned_driver_id = table.Column<Guid>(type: "uuid", nullable: true),
                    created_at = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    updated_at = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    cancelled_at = table.Column<DateTime>(type: "timestamp with time zone", nullable: true),
                    row_version = table.Column<byte[]>(type: "bytea", rowVersion: true, nullable: false),
                    version = table.Column<int>(type: "integer", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_ride_requests", x => x.id);
                    table.ForeignKey(
                        name: "FK_ride_requests_riders_rider_id",
                        column: x => x.rider_id,
                        principalTable: "riders",
                        principalColumn: "id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateTable(
                name: "trips",
                columns: table => new
                {
                    id = table.Column<Guid>(type: "uuid", nullable: false),
                    trip_id = table.Column<Guid>(type: "uuid", nullable: false),
                    ride_id = table.Column<Guid>(type: "uuid", nullable: false),
                    driver_id = table.Column<Guid>(type: "uuid", nullable: false),
                    rider_id = table.Column<Guid>(type: "uuid", nullable: false),
                    vehicle_id = table.Column<Guid>(type: "uuid", nullable: false),
                    status = table.Column<string>(type: "text", nullable: false),
                    pickup_latitude = table.Column<decimal>(type: "numeric", nullable: false),
                    pickup_longitude = table.Column<decimal>(type: "numeric", nullable: false),
                    dropoff_latitude = table.Column<decimal>(type: "numeric", nullable: false),
                    dropoff_longitude = table.Column<decimal>(type: "numeric", nullable: false),
                    current_latitude = table.Column<decimal>(type: "numeric", nullable: false),
                    current_longitude = table.Column<decimal>(type: "numeric", nullable: false),
                    started_at = table.Column<DateTime>(type: "timestamp with time zone", nullable: true),
                    completed_at = table.Column<DateTime>(type: "timestamp with time zone", nullable: true),
                    distance_traveled_meters = table.Column<decimal>(type: "numeric", nullable: false),
                    duration_seconds = table.Column<int>(type: "integer", nullable: false),
                    final_fare = table.Column<decimal>(type: "numeric", nullable: false),
                    created_at = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    updated_at = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    row_version = table.Column<byte[]>(type: "bytea", rowVersion: true, nullable: false),
                    version = table.Column<int>(type: "integer", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_trips", x => x.id);
                    table.ForeignKey(
                        name: "FK_trips_drivers_driver_id",
                        column: x => x.driver_id,
                        principalTable: "drivers",
                        principalColumn: "id",
                        onDelete: ReferentialAction.Restrict);
                    table.ForeignKey(
                        name: "FK_trips_riders_rider_id",
                        column: x => x.rider_id,
                        principalTable: "riders",
                        principalColumn: "id",
                        onDelete: ReferentialAction.Restrict);
                });

            migrationBuilder.CreateTable(
                name: "offers",
                columns: table => new
                {
                    id = table.Column<Guid>(type: "uuid", nullable: false),
                    offer_id = table.Column<Guid>(type: "uuid", nullable: false),
                    trip_id = table.Column<Guid>(type: "uuid", nullable: false),
                    driver_id = table.Column<Guid>(type: "uuid", nullable: false),
                    expires_at = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    status = table.Column<string>(type: "text", nullable: false),
                    created_at = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    updated_at = table.Column<DateTime>(type: "timestamp with time zone", nullable: true),
                    Version = table.Column<int>(type: "integer", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_offers", x => x.id);
                    table.ForeignKey(
                        name: "FK_offers_drivers_driver_id",
                        column: x => x.driver_id,
                        principalTable: "drivers",
                        principalColumn: "id",
                        onDelete: ReferentialAction.Cascade);
                    table.ForeignKey(
                        name: "FK_offers_trips_trip_id",
                        column: x => x.trip_id,
                        principalTable: "trips",
                        principalColumn: "id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateTable(
                name: "payments",
                columns: table => new
                {
                    id = table.Column<Guid>(type: "uuid", nullable: false),
                    payment_id = table.Column<Guid>(type: "uuid", nullable: false),
                    trip_id = table.Column<Guid>(type: "uuid", nullable: false),
                    amount = table.Column<decimal>(type: "numeric", nullable: false),
                    payment_method = table.Column<string>(type: "text", nullable: false),
                    status = table.Column<string>(type: "text", nullable: false),
                    transaction_id = table.Column<string>(type: "text", nullable: false),
                    created_at = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    updated_at = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    Version = table.Column<int>(type: "integer", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_payments", x => x.id);
                    table.ForeignKey(
                        name: "FK_payments_trips_trip_id",
                        column: x => x.trip_id,
                        principalTable: "trips",
                        principalColumn: "id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateIndex(
                name: "IX_drivers_email",
                table: "drivers",
                column: "email",
                unique: true);

            migrationBuilder.CreateIndex(
                name: "IX_drivers_is_available",
                table: "drivers",
                column: "is_available");

            migrationBuilder.CreateIndex(
                name: "IX_drivers_license_number",
                table: "drivers",
                column: "license_number",
                unique: true);

            migrationBuilder.CreateIndex(
                name: "IX_location_history_driver_id_timestamp",
                table: "location_history",
                columns: new[] { "driver_id", "timestamp" });

            migrationBuilder.CreateIndex(
                name: "IX_location_history_timestamp",
                table: "location_history",
                column: "timestamp");

            migrationBuilder.CreateIndex(
                name: "IX_offers_driver_id",
                table: "offers",
                column: "driver_id");

            migrationBuilder.CreateIndex(
                name: "IX_offers_expires_at",
                table: "offers",
                column: "expires_at");

            migrationBuilder.CreateIndex(
                name: "IX_offers_status",
                table: "offers",
                column: "status");

            migrationBuilder.CreateIndex(
                name: "IX_offers_trip_id_driver_id",
                table: "offers",
                columns: new[] { "trip_id", "driver_id" },
                unique: true);

            migrationBuilder.CreateIndex(
                name: "IX_outbox_events_created_at",
                table: "outbox_events",
                column: "created_at");

            migrationBuilder.CreateIndex(
                name: "IX_outbox_events_processed_at",
                table: "outbox_events",
                column: "processed_at");

            migrationBuilder.CreateIndex(
                name: "IX_payments_status",
                table: "payments",
                column: "status");

            migrationBuilder.CreateIndex(
                name: "IX_payments_transaction_id",
                table: "payments",
                column: "transaction_id",
                unique: true);

            migrationBuilder.CreateIndex(
                name: "IX_payments_trip_id",
                table: "payments",
                column: "trip_id",
                unique: true);

            migrationBuilder.CreateIndex(
                name: "IX_ride_requests_created_at",
                table: "ride_requests",
                column: "created_at");

            migrationBuilder.CreateIndex(
                name: "IX_ride_requests_rider_id",
                table: "ride_requests",
                column: "rider_id");

            migrationBuilder.CreateIndex(
                name: "IX_ride_requests_status",
                table: "ride_requests",
                column: "status");

            migrationBuilder.CreateIndex(
                name: "IX_riders_email",
                table: "riders",
                column: "email",
                unique: true);

            migrationBuilder.CreateIndex(
                name: "IX_riders_user_id",
                table: "riders",
                column: "user_id");

            migrationBuilder.CreateIndex(
                name: "IX_trips_created_at",
                table: "trips",
                column: "created_at");

            migrationBuilder.CreateIndex(
                name: "IX_trips_driver_id",
                table: "trips",
                column: "driver_id");

            migrationBuilder.CreateIndex(
                name: "IX_trips_rider_id",
                table: "trips",
                column: "rider_id");

            migrationBuilder.CreateIndex(
                name: "IX_trips_status",
                table: "trips",
                column: "status");
        }

        /// <inheritdoc />
        protected override void Down(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.DropTable(
                name: "location_history");

            migrationBuilder.DropTable(
                name: "offers");

            migrationBuilder.DropTable(
                name: "outbox_events");

            migrationBuilder.DropTable(
                name: "payments");

            migrationBuilder.DropTable(
                name: "ride_requests");

            migrationBuilder.DropTable(
                name: "trips");

            migrationBuilder.DropTable(
                name: "drivers");

            migrationBuilder.DropTable(
                name: "riders");
        }
    }
}
