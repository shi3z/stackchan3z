// CoreS3 GC0308 camera (pin map from the M5CoreS3 library, but initialized at runtime to avoid
// the static-init-order crash that library has with M5Unified).
#pragma once
#include <M5Unified.h>
#include "esp_camera.h"
#include "esp_log.h"

class CoreS3Camera {
 public:
  camera_fb_t* fb = nullptr;
  sensor_t* sensor = nullptr;

  bool begin() {
    camera_config_t c = {};
    c.pin_pwdn = -1; c.pin_reset = -1; c.pin_xclk = -1;   // StackChan: camera clock comes from an external 20 MHz crystal (official config: XCLK NC)
    c.pin_sscb_sda = 12; c.pin_sscb_scl = 11;
    c.pin_d7 = 47; c.pin_d6 = 48; c.pin_d5 = 16; c.pin_d4 = 15;
    c.pin_d3 = 42; c.pin_d2 = 41; c.pin_d1 = 40; c.pin_d0 = 39;
    c.pin_vsync = 46; c.pin_href = 38; c.pin_pclk = 45;
    c.xclk_freq_hz = 20000000;
    c.ledc_timer = LEDC_TIMER_0; c.ledc_channel = LEDC_CHANNEL_0;
    c.pixel_format = PIXFORMAT_RGB565; c.frame_size = FRAMESIZE_QVGA;
    c.jpeg_quality = 0; c.fb_count = 2; c.fb_location = CAMERA_FB_IN_PSRAM;
    c.grab_mode = CAMERA_GRAB_LATEST;
    c.sccb_i2c_port = M5.In_I2C.getPort();
    M5.In_I2C.release();
    if (esp_camera_init(&c) != ESP_OK) return false;
    // cam_hal logs "FB-OVF" on every overrun from its own small-stack task; printf there overflows the stack -> crash.
    esp_log_level_set("cam_hal", ESP_LOG_NONE);
    esp_log_level_set("camera", ESP_LOG_NONE);
    sensor = esp_camera_sensor_get();
    return sensor != nullptr;
  }
  bool get() { fb = esp_camera_fb_get(); return fb != nullptr; }
  void free() { if (fb) { esp_camera_fb_return(fb); fb = nullptr; } }
};
