#pragma once

#include <EpdFontFamily.h>
#include <HalDisplay.h>

namespace BidiUtils {
enum class BidiBaseDir : signed char { AUTO = -1, LTR = 0, RTL = 1 };
}  // namespace BidiUtils

class FontCacheManager;
class SdCardFont;

#include <array>
#include <cstring>
#include <map>
#include <string>
#include <vector>

#include "Bitmap.h"
#include "GrayFrame.h"

enum Color : uint8_t { Clear = 0x00, White = 0x01, LightGray = 0x05, DarkGray = 0x0A, Black = 0x10 };

class GfxRenderer {
 public:
  enum RenderMode { BW, BW_GRAY_BASE, GRAYSCALE_LSB, GRAYSCALE_MSB, GRAYSCALE_BOTH };

  enum class FrameOwner { LiveUi, ReaderBase, ReaderScratch, OffscreenBw, OffscreenSelector, OffscreenFrame, Loan };
  enum class CoveragePolicy { Collect, Suspend };

  enum Orientation {
    Portrait,                  // 480x800 logical coordinates (current default)
    LandscapeClockwise,        // 800x480 logical coordinates, rotated 180° (swap top/bottom)
    PortraitInverted,          // 480x800 logical coordinates, inverted
    LandscapeCounterClockwise  // 800x480 logical coordinates, native panel orientation
  };

 private:
  static constexpr size_t BW_BUFFER_CHUNK_SIZE = 8000;  // 8KB chunks to allow for non-contiguous memory

  HalDisplay& display;
  mutable RenderMode renderMode;
  Orientation orientation;
  bool fadingFix;
  uint8_t* frameBuffer = nullptr;
  uint16_t panelWidth = HalDisplay::DISPLAY_WIDTH;
  uint16_t panelHeight = HalDisplay::DISPLAY_HEIGHT;
  uint16_t panelWidthBytes = HalDisplay::DISPLAY_WIDTH_BYTES;
  uint32_t frameBufferSize = HalDisplay::BUFFER_SIZE;
  std::vector<uint8_t*> bwBufferChunks;
  std::map<int, EpdFontFamily> fontMap;
  mutable std::map<int, SdCardFont*> sdCardFonts_;
  mutable std::map<int, uint16_t> sdCardFontScales_;  // fontId -> 8.8 fixed point scale (256=1.0x)

  mutable FontCacheManager* fontCacheManager_ = nullptr;

  struct TargetBinding {
    uint8_t* buffer = nullptr;
    uint8_t* secondary = nullptr;
    int y0 = 0;
    int rows = 0;
    bool strip = false;
    bool clip = false;
    int x0 = 0;
    int clipY0 = 0;
    int x1 = 0;
    int y1 = 0;
    FrameOwner owner = FrameOwner::LiveUi;
    CoveragePolicy coverage = CoveragePolicy::Collect;
    GrayFrame tuple;
    bool* coherent = nullptr;
    uint32_t identity = 0;
    bool coherentOnEntry = false;
  };
  mutable TargetBinding target_;
  mutable TargetBinding stripSaved_;
  mutable RenderMode stripSavedMode_ = BW;
  mutable bool stripBound_ = false;
  mutable uint32_t targetGeneration_ = 0;
  uint8_t* liveL_ = nullptr;
  uint8_t* liveM_ = nullptr;
  bool uiGrayEnabled_ = false;
  bool allocationAttempted_ = false;
  mutable bool liveCoherent_ = true;
  mutable bool cancellationAccounted_ = false;
  mutable uint32_t workGeneration_ = 0;
  struct ReaderImport {
    bool active = false;
    uint32_t target = 0;
    uint32_t work = 0;
    Orientation orientation = Portrait;
    int x0 = 0;
    int x1 = 0;
    int y0 = 0;
    int y1 = 0;
    std::array<uint8_t, HalDisplay::DISPLAY_HEIGHT> coverage{};
  };
  mutable ReaderImport readerImport_;

  bool readerImportCurrent() const;
  bool replaceReaderRows(uint8_t* destination, uint8_t coverage, const uint8_t* source, size_t capacity, int yStart,
                         int rows) const;
  bool hasUiGray() const;
  bool submitUiGray(HalDisplay::RefreshMode refreshMode) const;

  GrayFrame activeTuple() const;
  bool accountCancellation() const;
  bool canSubmit() const;
  void invalidateTarget() const;
  void bindStrip(uint8_t* primary, uint8_t* secondary, int y0, int rows) const;

  std::map<int, int> fallbackFontMap_;
  std::map<int, int> builtinFallbackFontMap_;

  int resolveTextFontId(int fontId, const char* text, EpdFontFamily::Style style) const;

  void renderChar(const EpdFontFamily& fontFamily, uint32_t cp, int* x, int* y, bool pixelState,
                  EpdFontFamily::Style style) const;
  void freeBwBufferChunks();
  template <Color color>
  void drawPixelDither(int x, int y) const;
  template <Color color>
  void fillArc(int maxRadius, int cx, int cy, int xDir, int yDir) const;
  template <Color color>
  void fillRectImpl(int x, int y, int width, int height) const;

 public:
  explicit GfxRenderer(HalDisplay& halDisplay)
      : display(halDisplay), renderMode(BW), orientation(Portrait), fadingFix(false) {}
  ~GfxRenderer();
  GfxRenderer(const GfxRenderer&) = delete;
  GfxRenderer& operator=(const GfxRenderer&) = delete;

  bool setUiGrayEnabled(bool enabled);
  bool uiGrayEnabled() const { return uiGrayEnabled_; }
  bool liveFrameValid() const { return frameBuffer && liveCoherent_; }
  bool liveFrameNeedsRedraw() const { return !liveFrameValid(); }
  bool canCaptureLiveFrame() const;
  const uint8_t* getLiveGrayPlane(bool lsb) const {
    return uiGrayEnabled_ && canCaptureLiveFrame() ? (lsb ? liveL_ : liveM_) : nullptr;
  }
  FrameOwner getFrameOwner() const { return target_.owner; }
  uint32_t getTargetGeneration() const { return targetGeneration_; }
  bool isTargetCurrent(uint32_t generation) const { return frameBuffer && generation == targetGeneration_; }
  bool coverageEnabled() const;
  void setCoveragePolicy(CoveragePolicy policy) const { target_.coverage = policy; }
  bool drawCoverage(int x, int y, uint8_t coverage, bool blackInk = true) const;

  class ScopedTarget {
   public:
    ScopedTarget(const GfxRenderer& renderer, FrameOwner owner);
    ScopedTarget(const GfxRenderer& renderer, uint8_t* primary, int y0, int rows, uint8_t* secondary = nullptr);
    ScopedTarget(const GfxRenderer& renderer, GrayFrame tuple, bool& coherent);
    ~ScopedTarget();
    ScopedTarget(const ScopedTarget&) = delete;
    ScopedTarget& operator=(const ScopedTarget&) = delete;
    bool active() const { return active_; }
    void cancel();

   private:
    const GfxRenderer& renderer_;
    TargetBinding previous_;
    RenderMode previousMode_;
    TargetBinding previousStrip_;
    RenderMode previousStripMode_;
    bool previousStripBound_;
    bool active_ = false;
  };

  static constexpr int VIEWABLE_MARGIN_TOP = 9;
  static constexpr int VIEWABLE_MARGIN_RIGHT = 3;
  static constexpr int VIEWABLE_MARGIN_BOTTOM = 3;
  static constexpr int VIEWABLE_MARGIN_LEFT = 3;

  void begin();  // must be called right after display.begin()
  void insertFont(int fontId, EpdFontFamily font);
  void removeFont(int fontId) {
    fontMap.erase(fontId);
    sdCardFonts_.erase(fontId);
    sdCardFontScales_.erase(fontId);
  }
  void setFontCacheManager(FontCacheManager* m) { fontCacheManager_ = m; }
  FontCacheManager* getFontCacheManager() const { return fontCacheManager_; }
  bool isFontCacheScanning() const;
  const std::map<int, EpdFontFamily>& getFontMap() const { return fontMap; }
  void registerSdCardFont(int fontId, SdCardFont* font) { sdCardFonts_[fontId] = font; }
  void unregisterSdCardFont(int fontId) { removeFont(fontId); }
  void clearSdCardFonts() {
    sdCardFonts_.clear();
    sdCardFontScales_.clear();
  }
  void registerSdCardFontScale(int fontId, uint16_t scale) { sdCardFontScales_[fontId] = scale; }
  void clearSdCardFontScales() { sdCardFontScales_.clear(); }
  uint16_t getSdCardFontScale(int fontId) const {
    auto it = sdCardFontScales_.find(fontId);
    return (it != sdCardFontScales_.end()) ? it->second : 256;
  }
  const std::map<int, SdCardFont*>& getSdCardFonts() const { return sdCardFonts_; }
  bool isSdCardFont(int fontId) const { return sdCardFonts_.count(fontId) > 0; }
  void setFallbackFont(int primaryFontId, int fallbackFontId) { fallbackFontMap_[primaryFontId] = fallbackFontId; }
  void setBuiltinFallbackFont(int primaryFontId, int fallbackFontId) {
    builtinFallbackFontMap_[primaryFontId] = fallbackFontId;
    fallbackFontMap_[primaryFontId] = fallbackFontId;
  }
  void clearFallbackFonts() { fallbackFontMap_ = builtinFallbackFontMap_; }
  int resolveParagraphFontId(int primaryFontId, const std::vector<std::string>& words,
                             const std::vector<EpdFontFamily::Style>& styles) const;
  int getFallbackFontId(int primaryFontId) const;
  void ensureSdCardFontReady(int fontId, const char* utf8Text, uint8_t styleMask = 0x0F) const;
  void ensureSdCardFontReady(int fontId, const std::vector<std::string>& words, bool includeHyphen,
                             uint8_t styleMask = 0x0F) const;

  void setOrientation(const Orientation o) { orientation = o; }
  Orientation getOrientation() const { return orientation; }

  void setFadingFix(const bool enabled) { fadingFix = enabled; }

  int getScreenWidth() const;
  int getScreenHeight() const;
  void tapToLogical(float nx, float ny, int& outX, int& outY) const;
  void displayBuffer(HalDisplay::RefreshMode refreshMode = HalDisplay::FAST_REFRESH) const;
  void displayBufferAsync(HalDisplay::RefreshMode refreshMode = HalDisplay::FAST_REFRESH) const;
  bool refreshBusy() const;
  void waitRefreshComplete() const;
  bool supportsAsyncRefresh() const;
  void beginDisplayWork() const;
  void abortDisplayWork() const;
  bool displayWorkAborted() const;
  bool displayCommitted() const;
  void runDisplayMaintenance() const;
  bool hasPendingDisplayMaintenance() const;
  void displayControllerIdle() const;
  void invertScreen() const;
  void clearScreen(uint8_t color = 0xFF) const;
  void getOrientedViewableTRBL(int* outTop, int* outRight, int* outBottom, int* outLeft) const;

  void beginStripTarget(uint8_t* scratch, int stripY0, int stripRows) const;
  void beginDualStripTarget(uint8_t* lsb, uint8_t* msb, int stripY0, int stripRows) const;
  void endStripTarget() const;
  void setGrayscaleClipRect(int x, int y, int width, int height) const;
  void clearGrayscaleClipRect() const { target_.clip = false; }
  bool grayscaleClipEnabled() const { return target_.clip; }
  int grayscaleClipX0() const { return target_.x0; }
  int grayscaleClipY0() const { return target_.clipY0; }
  int grayscaleClipX1() const { return target_.x1; }
  int grayscaleClipY1() const { return target_.y1; }

  bool glyphIntersectsStrip(int x0, int y0, int x1, int y1) const;

  uint8_t* getWriteTarget() const { return frameBuffer ? (target_.strip ? target_.buffer : frameBuffer) : nullptr; }
  uint8_t* getSecondaryWriteTarget() const { return frameBuffer && target_.strip ? target_.secondary : nullptr; }
  int getWriteOriginY() const { return target_.strip ? target_.y0 : 0; }
  int getWriteRows() const { return target_.strip ? target_.rows : panelHeight; }

  void drawPixel(int x, int y, bool state = true) const;
  void drawGrayPixel(int x, int y, bool lsb, bool msb) const;
  void drawLine(int x1, int y1, int x2, int y2, bool state = true) const;
  void drawLine(int x1, int y1, int x2, int y2, int lineWidth, bool state) const;
  void drawArc(int maxRadius, int cx, int cy, int xDir, int yDir, int lineWidth, bool state) const;
  void drawRect(int x, int y, int width, int height, bool state = true) const;
  void drawRect(int x, int y, int width, int height, int lineWidth, bool state) const;
  void drawRoundedRect(int x, int y, int width, int height, int lineWidth, int cornerRadius, bool state) const;
  void drawRoundedRect(int x, int y, int width, int height, int lineWidth, int cornerRadius, bool roundTopLeft,
                       bool roundTopRight, bool roundBottomLeft, bool roundBottomRight, bool state) const;
  void maskRoundedRectOutsideCorners(int x, int y, int width, int height, int radius, Color color = Color::White) const;
  void fillRect(int x, int y, int width, int height, bool state = true) const;
  void fillRectDither(int x, int y, int width, int height, Color color) const;
  void fillRoundedRect(int x, int y, int width, int height, int cornerRadius, Color color) const;
  void fillRoundedRect(int x, int y, int width, int height, int cornerRadius, bool roundTopLeft, bool roundTopRight,
                       bool roundBottomLeft, bool roundBottomRight, Color color) const;
  void drawImage(const uint8_t bitmap[], int x, int y, int width, int height) const;
  void drawIcon(const uint8_t bitmap[], int x, int y, int size) const;
  void drawBitmap(const Bitmap& bitmap, int x, int y, int maxWidth, int maxHeight, float cropX = 0,
                  float cropY = 0) const;
  void drawBitmap1Bit(const Bitmap& bitmap, int x, int y, int maxWidth, int maxHeight) const;
  void fillPolygon(const int* xPoints, const int* yPoints, int numPoints, bool state = true) const;

  size_t readFramebufferRegion(int x, int y, int w, int h, uint8_t* dst, size_t dstCapacity) const;
  void writeFramebufferRegion(int x, int y, int w, int h, const uint8_t* src);

  int getTextWidth(int fontId, const char* text, EpdFontFamily::Style style = EpdFontFamily::REGULAR,
                   BidiUtils::BidiBaseDir baseDir = BidiUtils::BidiBaseDir::AUTO) const;
  void drawCenteredText(int fontId, int y, const char* text, bool black = true,
                        EpdFontFamily::Style style = EpdFontFamily::REGULAR,
                        BidiUtils::BidiBaseDir baseDir = BidiUtils::BidiBaseDir::AUTO) const;
  void drawText(int fontId, int x, int y, const char* text, bool black = true,
                EpdFontFamily::Style style = EpdFontFamily::REGULAR,
                BidiUtils::BidiBaseDir baseDir = BidiUtils::BidiBaseDir::AUTO) const;
  int getSpaceWidth(int fontId, EpdFontFamily::Style style = EpdFontFamily::REGULAR) const;
  int getSpaceAdvance(int fontId, uint32_t leftCp, uint32_t rightCp, EpdFontFamily::Style style) const;
  int getKerning(int fontId, uint32_t leftCp, uint32_t rightCp, EpdFontFamily::Style style) const;
  int getTextAdvanceX(int fontId, const char* text, EpdFontFamily::Style style) const;
  int getFontAscenderSize(int fontId) const;
  int getLineHeight(int fontId) const;
  int getLineHeight(int fontId, float compression) const;
  int getLineHeightForText(int fontId, const char* text,
                           EpdFontFamily::Style style = EpdFontFamily::REGULAR) const;
  std::string truncatedText(int fontId, const char* text, int maxWidth,
                            EpdFontFamily::Style style = EpdFontFamily::REGULAR) const;
  std::vector<std::string> wrappedText(int fontId, const char* text, int maxWidth, int maxLines,
                                       EpdFontFamily::Style style = EpdFontFamily::REGULAR) const;

  void drawTextRotated90CW(int fontId, int x, int y, const char* text, bool black = true,
                           EpdFontFamily::Style style = EpdFontFamily::REGULAR) const;
  int getTextHeight(int fontId) const;

  void setRenderMode(const RenderMode mode) { this->renderMode = mode; }
  RenderMode getRenderMode() const { return renderMode; }
  void preconditionGrayscale() const;
  void preconditionGrayscale(int x, int y, int w, int h) const;
  void displayGrayscaleBase(HalDisplay::RefreshMode fallback = HalDisplay::HALF_REFRESH) const;
  bool beginReaderImport(int x, int y, int width, int height) const;
  bool importReaderPlane(bool lsb, const uint8_t* source, size_t capacity, int yStart, int rows) const;
  bool restoreReaderBase(const uint8_t* source, size_t capacity, int yStart, int rows) const;
  bool finishReaderImport() const;
  void copyGrayscaleLsbBuffers() const;
  void copyGrayscaleMsbBuffers() const;
  void displayGrayBuffer() const;
  void displayGrayCalibration(int customX, int customY, int customW, int customH) const;

  void writeGrayscalePlaneStrip(bool lsbPlane, const uint8_t* scratch, int yStart, int numRows) const;
  bool supportsBusyGrayscaleStaging() const;
  void prepareGrayscaleTarget() const;
  bool supportsStripGrayscale() const;
  bool storeBwBuffer();    // Returns true if buffer was stored successfully
  void restoreBwBuffer();  // Restore and free the stored buffer
  void cleanupGrayscaleWithFrameBuffer() const;

  const uint8_t* getGlyphBitmap(const EpdFontData* fontData, const EpdGlyph* glyph) const;

  void releaseFrameBufferForBuild();
  bool restoreFrameBufferAfterBuild();
  bool hasFrameBuffer() const { return frameBuffer != nullptr; }

  class FrameBufferLoan {
   public:
    explicit FrameBufferLoan(GfxRenderer& renderer);
    ~FrameBufferLoan() { end(); }
    void end();
    FrameBufferLoan(const FrameBufferLoan&) = delete;
    FrameBufferLoan& operator=(const FrameBufferLoan&) = delete;

   private:
    GfxRenderer& renderer_;
    bool active_ = false;
  };

  uint8_t* getFrameBuffer() const;
  size_t getBufferSize() const;
  uint16_t getDisplayWidth() const { return panelWidth; }
  uint16_t getDisplayHeight() const { return panelHeight; }
  uint16_t getDisplayWidthBytes() const { return panelWidthBytes; }

  size_t getRegionByteSize(int logicalX, int logicalY, int logicalW, int logicalH) const;
  bool copyRegionToBuffer(int logicalX, int logicalY, int logicalW, int logicalH, uint8_t* buf, size_t bufSize) const;
  bool copyBufferToRegion(int logicalX, int logicalY, int logicalW, int logicalH, const uint8_t* buf,
                          size_t bufSize) const;
};
