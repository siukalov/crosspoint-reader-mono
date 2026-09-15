#include "GfxRenderer.h"

#include <BidiUtils.h>
#include <BuildScratch.h>
#include <FontDecompressor.h>
#include <HalGPIO.h>
#include <Logging.h>
#include <SdCardFont.h>
#include <Utf8.h>
#include <esp_heap_caps.h>

#include <algorithm>

#include "FontCacheManager.h"

namespace {

uint8_t resolveSdCardStyle(const SdCardFont& font, const EpdFontFamily::Style style) {
  return font.resolveStyle(static_cast<uint8_t>(style));
}

constexpr uint8_t AA_GRAY_LOW = 32;  // below this the pixel stays background
constexpr uint8_t AA_GRAY_HIGH = 128;
constexpr uint8_t AA_BLACK = 224;  // at or above this the pixel renders black

constexpr uint8_t quantiseCoverage(const uint8_t alpha) {
  return alpha < AA_GRAY_LOW ? 0 : alpha < AA_GRAY_HIGH ? 1 : alpha < AA_BLACK ? 2 : 3;
}

bool collectGlyphCoverage(const GfxRenderer& renderer, const int x, const int y, const uint8_t coverage,
                          const bool blackInk) {
  if (renderer.isFontCacheScanning()) return true;
  if (!renderer.coverageEnabled()) return false;
  // A bound coverage target must not fall through after a rejected write.
  renderer.drawCoverage(x, y, coverage, blackInk);
  return true;
}
}  // namespace

namespace {
const char* resolveVisualText(const char* text, std::string& visualBuffer, BidiUtils::BidiBaseDir baseDir);

void appendShapedRtlTokens(const char* text, std::string& shapedOut) {
  const auto isBreak = [](const char c) { return c == ' ' || c == '\n' || c == '\r' || c == '\t'; };
  std::string token;
  std::string visual;
  const char* p = text;
  while (*p) {
    while (*p && isBreak(*p)) ++p;
    const char* start = p;
    bool hasRtlBytes = false;
    while (*p && !isBreak(*p)) {
      const auto b = static_cast<unsigned char>(*p);
      hasRtlBytes = hasRtlBytes || (b >= 0xD6 && b <= 0xDB);
      ++p;
    }
    if (!hasRtlBytes) continue;
    token.assign(start, p - start);
    if (BidiUtils::applyBidiVisual(token.c_str(), visual, static_cast<int>(BidiUtils::BidiBaseDir::AUTO))) {
      shapedOut += visual;
    }
  }
}
}  // namespace

const uint8_t* GfxRenderer::getGlyphBitmap(const EpdFontData* fontData, const EpdGlyph* glyph) const {
  if (fontData->groups != nullptr) {
    auto* fd = fontCacheManager_ ? fontCacheManager_->getDecompressor() : nullptr;
    if (!fd) {
      LOG_ERR("GFX", "Compressed font but no FontDecompressor set");
      return nullptr;
    }
    uint32_t glyphIndex = static_cast<uint32_t>(glyph - fontData->glyph);
    return fd->getBitmap(fontData, glyph, glyphIndex);
  }
  if (fontData->glyphBitmapHandler) {
    return fontData->glyphBitmapHandler(fontData->glyphMissCtx, glyph);
  }
  if (fontData->glyphMissCtx) {
    auto* sdFont = SdCardFont::fromMissCtx(fontData->glyphMissCtx);
    if (sdFont->isOverflowGlyph(glyph)) {
      return sdFont->getOverflowBitmap(glyph);  // may be nullptr for zero-width glyphs
    }
  }
  return &fontData->bitmap[glyph->dataOffset];
}

void GfxRenderer::ensureSdCardFontReady(int fontId, const char* utf8Text, uint8_t styleMask) const {
  auto it = sdCardFonts_.find(fontId);
  if (it != sdCardFonts_.end()) {
    std::string shaped;
    appendShapedRtlTokens(utf8Text, shaped);
    int missed = it->second->buildAdvanceTable(utf8Text, styleMask, shaped.empty() ? nullptr : shaped.c_str());
    if (missed > 0) {
      LOG_DBG("GFX", "ensureSdCardFontReady: %d glyph(s) not found", missed);
    }
  }
}

void GfxRenderer::ensureSdCardFontReady(int fontId, const std::vector<std::string>& words, bool includeHyphen,
                                        uint8_t styleMask) const {
  auto it = sdCardFonts_.find(fontId);
  if (it != sdCardFonts_.end()) {
    std::string shaped;
    for (const auto& w : words) {
      appendShapedRtlTokens(w.c_str(), shaped);
    }
    int missed =
        it->second->buildAdvanceTable(words, includeHyphen, styleMask, shaped.empty() ? nullptr : shaped.c_str());
    if (missed > 0) {
      LOG_DBG("GFX", "ensureSdCardFontReady: %d glyph(s) not found", missed);
    }
  }
}

void GfxRenderer::begin() {
  frameBuffer = display.getFrameBuffer();
  ++targetGeneration_;
  if (!frameBuffer) {
    LOG_ERR("GFX", "!! No framebuffer");
    assert(false);
  }
  panelWidth = display.getDisplayWidth();
  panelHeight = display.getDisplayHeight();
  panelWidthBytes = display.getDisplayWidthBytes();
  frameBufferSize = display.getBufferSize();
  bwBufferChunks.assign((frameBufferSize + BW_BUFFER_CHUNK_SIZE - 1) / BW_BUFFER_CHUNK_SIZE, nullptr);
}

GfxRenderer::~GfxRenderer() {
  freeBwBufferChunks();
  heap_caps_free(liveL_);
  heap_caps_free(liveM_);
}

bool GfxRenderer::setUiGrayEnabled(const bool enabled) {
  if (!frameBuffer || target_.owner != FrameOwner::LiveUi || target_.strip) return false;
  if (enabled && !allocationAttempted_) {
    allocationAttempted_ = true;
    liveL_ = static_cast<uint8_t*>(heap_caps_malloc(frameBufferSize, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    liveM_ = static_cast<uint8_t*>(heap_caps_malloc(frameBufferSize, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (!liveL_ || !liveM_) {
      heap_caps_free(liveL_);
      heap_caps_free(liveM_);
      liveL_ = liveM_ = nullptr;
      LOG_ERR("GFX", "UI gray allocation failed (%u bytes per plane); retaining binary UI and reader AA",
              frameBufferSize);
    }
  }
  if (enabled && (!liveL_ || !liveM_)) return false;
  if (uiGrayEnabled_ != enabled) {
    freeBwBufferChunks();
    uiGrayEnabled_ = enabled;
    ++restoreEpoch_;
    liveCoherent_ = false;
    ++targetGeneration_;
  }
  return true;
}

GrayFrame GfxRenderer::activeTuple() const {
  if (!frameBuffer) return {};
  if (target_.owner == FrameOwner::OffscreenFrame) return target_.tuple;
  if (!uiGrayEnabled_ || target_.owner != FrameOwner::LiveUi || target_.strip) return {};
  return {{frameBuffer, frameBufferSize},
          {liveL_, frameBufferSize},
          {liveM_, frameBufferSize},
          panelWidth,
          panelHeight,
          panelWidthBytes};
}

bool GfxRenderer::coverageEnabled() const {
  return target_.coverage == CoveragePolicy::Collect && activeTuple().valid();
}

void GfxRenderer::invalidateTarget() const {
  if (target_.owner == FrameOwner::OffscreenFrame) {
    if (target_.coherent) *target_.coherent = false;
  } else if (target_.owner == FrameOwner::LiveUi || target_.owner == FrameOwner::ReaderBase ||
             target_.owner == FrameOwner::ReaderScratch) {
    ++restoreEpoch_;
    liveCoherent_ = false;
    readerImport_.active = false;
    target_.coherentOnEntry = false;
  }
}

bool GfxRenderer::accountCancellation() const {
  if (!display.postRefreshAborted()) return false;
  if (!cancellationAccounted_) {
    invalidateTarget();
    cancellationAccounted_ = true;
  }
  return true;
}

bool GfxRenderer::canCaptureLiveFrame() const {
  if (accountCancellation()) return false;
  return frameBuffer && !target_.strip && target_.owner == FrameOwner::LiveUi && liveCoherent_;
}

bool GfxRenderer::canSubmit() const {
  if (!frameBuffer || target_.strip || (target_.owner != FrameOwner::LiveUi && target_.owner != FrameOwner::ReaderBase))
    return false;
  if (accountCancellation()) return false;
  return liveCoherent_;
}

GfxRenderer::ScopedTarget::ScopedTarget(const GfxRenderer& renderer, FrameOwner owner)
    : renderer_(renderer),
      previous_(renderer.target_),
      previousMode_(renderer.renderMode),
      previousStrip_(renderer.stripSaved_),
      previousStripMode_(renderer.stripSavedMode_),
      previousStripBound_(renderer.stripBound_) {
  if (!renderer.frameBuffer ||
      (owner != FrameOwner::LiveUi && owner != FrameOwner::ReaderBase && owner != FrameOwner::ReaderScratch))
    return;
  renderer_.target_ = {};
  renderer_.target_.owner = owner;
  renderer_.target_.coverage = owner == FrameOwner::LiveUi ? CoveragePolicy::Collect : CoveragePolicy::Suspend;
  renderer_.target_.coherentOnEntry = renderer_.liveCoherent_;
  if (owner == FrameOwner::ReaderScratch && renderer_.uiGrayEnabled_) renderer_.liveCoherent_ = false;
  renderer_.target_.identity = ++renderer_.targetGeneration_;
  identity_ = renderer_.target_.identity;
  active_ = true;
}

GfxRenderer::ScopedTarget::ScopedTarget(const GfxRenderer& renderer, uint8_t* primary, int y0, int rows,
                                        uint8_t* secondary)
    : renderer_(renderer),
      previous_(renderer.target_),
      previousMode_(renderer.renderMode),
      previousStrip_(renderer.stripSaved_),
      previousStripMode_(renderer.stripSavedMode_),
      previousStripBound_(renderer.stripBound_) {
  if (!renderer.frameBuffer || !primary || rows <= 0 || y0 < 0 || y0 > renderer.panelHeight - rows) return;
  renderer_.bindStrip(primary, secondary, y0, rows);
  identity_ = renderer_.target_.identity;
  active_ = true;
}

GfxRenderer::ScopedTarget::ScopedTarget(const GfxRenderer& renderer, GrayFrame tuple, bool& coherent)
    : renderer_(renderer),
      previous_(renderer.target_),
      previousMode_(renderer.renderMode),
      previousStrip_(renderer.stripSaved_),
      previousStripMode_(renderer.stripSavedMode_),
      previousStripBound_(renderer.stripBound_) {
  if (!renderer.frameBuffer || !tuple.valid() || tuple.b().data == renderer.frameBuffer ||
      tuple.l().data == renderer.frameBuffer || tuple.m().data == renderer.frameBuffer ||
      tuple.width() != renderer.panelWidth || tuple.stride() != renderer.panelWidthBytes ||
      tuple.y0() > renderer.panelHeight || tuple.rows() > renderer.panelHeight - tuple.y0())
    return;
  renderer_.bindStrip(tuple.b().data, nullptr, tuple.y0(), tuple.rows());
  renderer_.target_.owner = FrameOwner::OffscreenFrame;
  renderer_.target_.coverage = CoveragePolicy::Collect;
  renderer_.target_.tuple = tuple;
  renderer_.target_.coherent = &coherent;
  identity_ = renderer_.target_.identity;
  active_ = true;
}

GfxRenderer::ScopedTarget::~ScopedTarget() {
  if (!active_) return;
  renderer_.accountCancellation();
  if (renderer_.bwScratchOwner_ == identity_) renderer_.freeBwBufferChunks();
  renderer_.target_ = previous_;
  renderer_.renderMode = previousMode_;
  renderer_.stripSaved_ = previousStrip_;
  renderer_.stripSavedMode_ = previousStripMode_;
  renderer_.stripBound_ = previousStripBound_;
  ++renderer_.targetGeneration_;
}

void GfxRenderer::ScopedTarget::cancel() {
  if (active_) renderer_.invalidateTarget();
}

void GfxRenderer::releaseFrameBufferForBuild() {
  if (!frameBuffer || target_.strip || target_.owner != FrameOwner::LiveUi) return;
  uint32_t size = 0;
  uint8_t* scratch = display.lendFrameBufferStorage(&size);
  if (!scratch) return;
  freeBwBufferChunks();
  frameBuffer = nullptr;
  target_.owner = FrameOwner::Loan;
  ++restoreEpoch_;
  liveCoherent_ = false;
  ++targetGeneration_;
  buildscratch::lend(scratch, size);
}

bool GfxRenderer::restoreFrameBufferAfterBuild() {
  if (target_.owner != FrameOwner::Loan) return frameBuffer != nullptr;
  buildscratch::reclaim();
  display.returnFrameBufferStorage();
  frameBuffer = display.getFrameBuffer();
  if (liveL_) memset(liveL_, 0, frameBufferSize);
  if (liveM_) memset(liveM_, 0, frameBufferSize);
  target_.owner = FrameOwner::LiveUi;
  liveCoherent_ = false;
  ++targetGeneration_;
  return frameBuffer != nullptr;
}

GfxRenderer::FrameBufferLoan::FrameBufferLoan(GfxRenderer& renderer) : renderer_(renderer) {
  if (!renderer_.hasFrameBuffer()) return;
  renderer_.releaseFrameBufferForBuild();
  active_ = !renderer_.hasFrameBuffer();
}

void GfxRenderer::FrameBufferLoan::end() {
  if (!active_) return;
  active_ = false;
  if (!renderer_.restoreFrameBufferAfterBuild()) {
    LOG_ERR("GFX", "Framebuffer restore failed - restarting");
    ESP.restart();
  }
}

bool GfxRenderer::isFontCacheScanning() const { return fontCacheManager_ && fontCacheManager_->isScanning(); }

void GfxRenderer::insertFont(const int fontId, EpdFontFamily font) {
  auto result = fontMap.insert({fontId, font});
  if (!result.second) {
    LOG_ERR("GFX", "Font ID %d already registered, ignoring duplicate", fontId);
  }
}

int GfxRenderer::resolveTextFontId(const int fontId, const char* text, const EpdFontFamily::Style style) const {
  if (fallbackFontMap_.empty() || text == nullptr || *text == '\0') {
    return fontId;
  }
  const auto fbIt = fallbackFontMap_.find(fontId);
  if (fbIt == fallbackFontMap_.end()) {
    return fontId;  // no fallback registered for this font
  }
  const int fallbackFontId = fbIt->second;
  const auto fontIt = fontMap.find(fontId);
  const auto fallbackIt = fontMap.find(fallbackFontId);
  if (fontIt == fontMap.end() || fallbackIt == fontMap.end()) {
    return fontId;  // unknown primary or fallback not loaded — let the caller handle it
  }
  const EpdFontFamily& primary = fontIt->second;
  const EpdFontFamily& fallback = fallbackIt->second;
  const char* cursor = text;
  uint32_t cp;
  while ((cp = utf8NextCodepoint(reinterpret_cast<const uint8_t**>(&cursor)))) {
    if (utf8IsCjkCodepoint(cp) && !primary.hasCodepoint(cp, style) && fallback.hasCodepoint(cp, style)) {
      return fallbackFontId;
    }
  }
  return fontId;
}

int GfxRenderer::resolveParagraphFontId(const int primaryFontId, const std::vector<std::string>& words,
                                        const std::vector<EpdFontFamily::Style>& styles) const {
  const size_t count = std::min(words.size(), styles.size());
  for (size_t i = 0; i < count; ++i) {
    const int resolved = resolveTextFontId(primaryFontId, words[i].c_str(), styles[i]);
    if (resolved != primaryFontId) return resolved;
  }
  return primaryFontId;
}

int GfxRenderer::getFallbackFontId(const int primaryFontId) const {
  const auto fallback = fallbackFontMap_.find(primaryFontId);
  if (fallback == fallbackFontMap_.end() || fontMap.find(fallback->second) == fontMap.end()) {
    return primaryFontId;
  }
  return fallback->second;
}

static inline void rotateCoordinates(const GfxRenderer::Orientation orientation, const int x, const int y, int* phyX,
                                     int* phyY, const uint16_t panelWidth, const uint16_t panelHeight) {
  switch (orientation) {
    case GfxRenderer::Portrait: {
      *phyX = y;
      *phyY = panelHeight - 1 - x;
      break;
    }
    case GfxRenderer::LandscapeClockwise: {
      *phyX = panelWidth - 1 - x;
      *phyY = panelHeight - 1 - y;
      break;
    }
    case GfxRenderer::PortraitInverted: {
      *phyX = panelWidth - 1 - y;
      *phyY = x;
      break;
    }
    case GfxRenderer::LandscapeCounterClockwise: {
      *phyX = x;
      *phyY = y;
      break;
    }
  }
}

struct AlignedMemRect {
  uint16_t x = 0;
  uint16_t y = 0;
  uint16_t w = 0;
  uint16_t h = 0;
  bool valid = false;
};

static AlignedMemRect screenRectToAlignedMemRect(GfxRenderer::Orientation orientation, int sx, int sy, int sw, int sh,
                                                 uint16_t panelWidth, uint16_t panelHeight) {
  AlignedMemRect out;
  if (sw <= 0 || sh <= 0) return out;

  int x0, y0, x1, y1;
  rotateCoordinates(orientation, sx, sy, &x0, &y0, panelWidth, panelHeight);
  rotateCoordinates(orientation, sx + sw - 1, sy + sh - 1, &x1, &y1, panelWidth, panelHeight);

  const int memXLo = std::min(x0, x1);
  const int memYLo = std::min(y0, y1);
  const int memXHi = std::max(x0, x1) + 1;  // exclusive upper bound
  const int memYHi = std::max(y0, y1) + 1;

  int alignedXLo = memXLo & ~0x7;        // round down
  int alignedXHi = (memXHi + 7) & ~0x7;  // round up

  if (alignedXLo < 0) alignedXLo = 0;
  if (alignedXHi > panelWidth) alignedXHi = panelWidth;
  int clampedYLo = memYLo;
  int clampedYHi = memYHi;
  if (clampedYLo < 0) clampedYLo = 0;
  if (clampedYHi > panelHeight) clampedYHi = panelHeight;

  if (alignedXHi <= alignedXLo || clampedYHi <= clampedYLo) return out;

  out.x = static_cast<uint16_t>(alignedXLo);
  out.y = static_cast<uint16_t>(clampedYLo);
  out.w = static_cast<uint16_t>(alignedXHi - alignedXLo);
  out.h = static_cast<uint16_t>(clampedYHi - clampedYLo);
  out.valid = true;
  return out;
}

enum class TextRotation { None, Rotated90CW };

namespace {
struct SnapshotLayout {
  AlignedMemRect rect;
  size_t rowBytes = 0;
  size_t planeBytes = 0;
  size_t totalBytes = 0;
};

SnapshotLayout snapshotLayout(GfxRenderer::Orientation orientation, int x, int y, int width, int height,
                              uint16_t panelWidth, uint16_t panelHeight, uint16_t panelStride, size_t planeCount) {
  if (width <= 0 || height <= 0 || !panelWidth || !panelHeight ||
      panelStride < (static_cast<size_t>(panelWidth) + 7) / 8)
    return {};
  const bool portrait = orientation == GfxRenderer::Portrait || orientation == GfxRenderer::PortraitInverted;
  if (!portrait && orientation != GfxRenderer::LandscapeClockwise &&
      orientation != GfxRenderer::LandscapeCounterClockwise)
    return {};
  const int64_t logicalWidth = portrait ? panelHeight : panelWidth;
  const int64_t logicalHeight = portrait ? panelWidth : panelHeight;
  const int64_t x0 = std::max<int64_t>(0, x);
  const int64_t y0 = std::max<int64_t>(0, y);
  const int64_t x1 = std::min<int64_t>(logicalWidth, static_cast<int64_t>(x) + width);
  const int64_t y1 = std::min<int64_t>(logicalHeight, static_cast<int64_t>(y) + height);
  if (x0 >= x1 || y0 >= y1) return {};
  SnapshotLayout layout;
  layout.rect = screenRectToAlignedMemRect(orientation, x0, y0, x1 - x0, y1 - y0, panelWidth, panelHeight);
  if (!layout.rect.valid) return {};
  layout.rowBytes = (static_cast<size_t>(layout.rect.w) + 7) / 8;
  if (layout.rect.x == 0 && layout.rect.w == panelWidth) layout.rowBytes = panelStride;
  if (layout.rowBytes > std::numeric_limits<size_t>::max() / layout.rect.h) return {};
  layout.planeBytes = layout.rowBytes * layout.rect.h;
  if (layout.planeBytes > std::numeric_limits<size_t>::max() / planeCount) return {};
  layout.totalBytes = layout.planeBytes * planeCount;
  return layout;
}

bool addressRangeFits(const uint8_t* data, size_t bytes) {
  return data && bytes <= std::numeric_limits<uintptr_t>::max() - reinterpret_cast<uintptr_t>(data);
}

bool addressRangesOverlap(const uint8_t* first, size_t firstBytes, const uint8_t* second, size_t secondBytes) {
  const auto firstAddress = reinterpret_cast<uintptr_t>(first);
  const auto secondAddress = reinterpret_cast<uintptr_t>(second);
  return firstAddress < secondAddress + secondBytes && secondAddress < firstAddress + firstBytes;
}
bool snapshotGeometryValid(const GfxRenderer::FrameSnapshot& source) {
  const auto& planes = source.planes;
  if (!planes.width() || !planes.rows() || source.physicalByteX >= source.panelStride ||
      source.physicalByteX > source.panelWidth / 8 || planes.y0() >= source.panelHeight)
    return false;
  const size_t remainingWidth = source.panelWidth - source.physicalByteX * 8;
  if (planes.width() > remainingWidth || planes.rows() > source.panelHeight - planes.y0()) return false;
  if (planes.width() % 8 && planes.width() != remainingWidth) return false;
  const size_t rowBytes =
      source.physicalByteX == 0 && planes.width() == source.panelWidth ? source.panelStride : (planes.width() + 7) / 8;
  return planes.stride() == rowBytes;
}

GfxRenderer::FrameResult validateSnapshotPlanes(const GfxRenderer::FrameSnapshot& source,
                                                uint8_t* const destinations[3], size_t destinationBytes,
                                                size_t planeCount) {
  using Result = GfxRenderer::FrameResult;
  const GrayFrame::Plane sources[] = {source.planes.b(), source.planes.l(), source.planes.m()};
  const size_t sourceBytes = source.planes.stride() * source.planes.rows();
  for (size_t plane = 0; plane < planeCount; ++plane) {
    if (!addressRangeFits(sources[plane].data, sourceBytes)) return Result::InvalidSource;
    if (sources[plane].capacity < sourceBytes) return Result::ShortCapacity;
  }
  for (size_t plane = 0; plane < 3; ++plane) {
    if (!destinations[plane] && plane >= planeCount) continue;
    if (!addressRangeFits(destinations[plane], destinationBytes)) return Result::InvalidLive;
    for (size_t other = 0; other < planeCount; ++other) {
      if (addressRangesOverlap(sources[other].data, sourceBytes, destinations[plane], destinationBytes))
        return Result::InvalidSource;
    }
  }
  for (size_t plane = 0; plane < planeCount; ++plane) {
    for (size_t other = plane + 1; other < planeCount; ++other) {
      if (addressRangesOverlap(sources[plane].data, sourceBytes, sources[other].data, sourceBytes))
        return Result::InvalidSource;
    }
  }
  return Result::Ok;
}
}  // namespace

static void renderCharScaled(const GfxRenderer& renderer, GfxRenderer::RenderMode renderMode,
                             const EpdFontFamily& fontFamily, const uint32_t cp, int cursorX, int cursorY,
                             const bool pixelState, const EpdFontFamily::Style style) {
  const EpdGlyph* glyph = fontFamily.getGlyph(cp, style);
  if (!glyph) return;

  const EpdFontData* fontData = fontFamily.getData(style);
  const uint8_t* bitmap = renderer.getGlyphBitmap(fontData, glyph);
  if (!bitmap) return;

  const int srcW = glyph->width;
  const int srcH = glyph->height;
  const int dstW = (srcW + 1) / 2;  // ceil so odd-width glyphs aren't clipped
  const int dstH = (srcH + 1) / 2;
  const int baseX = cursorX + glyph->left / 2;
  const int baseY = cursorY - glyph->top / 2;

  if (fontData->glyphBitmapBpp == 8) {
    for (int dstY = 0; dstY < dstH; dstY++) {
      const int srcY = dstY * 2;
      for (int dstX = 0; dstX < dstW; dstX++) {
        const int srcX = dstX * 2;
        uint16_t coverage = 0;
        uint8_t samples = 0;
        for (int sampleY = 0; sampleY < 2 && srcY + sampleY < srcH; sampleY++) {
          for (int sampleX = 0; sampleX < 2 && srcX + sampleX < srcW; sampleX++) {
            coverage += bitmap[(srcY + sampleY) * srcW + srcX + sampleX];
            ++samples;
          }
        }
        const uint8_t alpha = static_cast<uint8_t>((coverage + samples / 2) / samples);
        const uint8_t raw = quantiseCoverage(alpha);
        if (collectGlyphCoverage(renderer, baseX + dstX, baseY + dstY, raw, pixelState)) continue;
        if ((renderMode == GfxRenderer::BW && raw >= 2) || (renderMode == GfxRenderer::BW_GRAY_BASE && raw >= 2)) {
          renderer.drawPixel(baseX + dstX, baseY + dstY, pixelState);
        } else if (renderMode == GfxRenderer::GRAYSCALE_MSB && (raw == 1 || raw == 2)) {
          renderer.drawPixel(baseX + dstX, baseY + dstY, false);
        } else if (renderMode == GfxRenderer::GRAYSCALE_LSB && raw == 2) {
          renderer.drawPixel(baseX + dstX, baseY + dstY, false);
        } else if (renderMode == GfxRenderer::GRAYSCALE_BOTH && (raw == 1 || raw == 2)) {
          renderer.drawGrayPixel(baseX + dstX, baseY + dstY, raw == 2, true);
        }
      }
    }
  } else if (fontData->is2Bit) {
    for (int dstY = 0; dstY < dstH; dstY++) {
      const int srcY = dstY * 2;
      for (int dstX = 0; dstX < dstW; dstX++) {
        const int srcX = dstX * 2;
        uint8_t coverage = 0;
        uint8_t samples = 0;
        for (int sampleY = 0; sampleY < 2 && srcY + sampleY < srcH; sampleY++) {
          for (int sampleX = 0; sampleX < 2 && srcX + sampleX < srcW; sampleX++) {
            const int pos = (srcY + sampleY) * srcW + srcX + sampleX;
            const uint8_t byte = bitmap[pos >> 2];
            const uint8_t raw = (byte >> ((3 - (pos & 3)) * 2)) & 0x3;
            coverage += raw;
            ++samples;
          }
        }

        uint8_t raw = 0;
        if (coverage > 0) {
          const uint8_t average = static_cast<uint8_t>((coverage + samples / 2) / samples);
          raw = static_cast<uint8_t>(std::min<int>(3, average + (coverage >= 3 ? 1 : 0)));
        }
        if (collectGlyphCoverage(renderer, baseX + dstX, baseY + dstY, raw, pixelState)) continue;
        if ((renderMode == GfxRenderer::BW && raw > 0) || (renderMode == GfxRenderer::BW_GRAY_BASE && raw >= 2)) {
          renderer.drawPixel(baseX + dstX, baseY + dstY, pixelState);
        } else if (renderMode == GfxRenderer::GRAYSCALE_MSB && (raw == 1 || raw == 2)) {
          renderer.drawPixel(baseX + dstX, baseY + dstY, false);
        } else if (renderMode == GfxRenderer::GRAYSCALE_LSB && raw == 2) {
          renderer.drawPixel(baseX + dstX, baseY + dstY, false);
        } else if (renderMode == GfxRenderer::GRAYSCALE_BOTH && (raw == 1 || raw == 2)) {
          renderer.drawGrayPixel(baseX + dstX, baseY + dstY, raw == 2, true);
        }
      }
    }
  } else {
    for (int dstY = 0; dstY < dstH; dstY++) {
      const int srcY = dstY * 2;
      for (int dstX = 0; dstX < dstW; dstX++) {
        const int srcX = dstX * 2;
        bool hasInk = false;
        for (int sampleY = 0; sampleY < 2 && srcY + sampleY < srcH; sampleY++) {
          for (int sampleX = 0; sampleX < 2 && srcX + sampleX < srcW; sampleX++) {
            const int pos = (srcY + sampleY) * srcW + srcX + sampleX;
            const uint8_t byte = bitmap[pos >> 3];
            const uint8_t bit = 7 - (pos & 7);
            if ((byte >> bit) & 1) {
              hasInk = true;
            }
          }
        }
        if (hasInk && !collectGlyphCoverage(renderer, baseX + dstX, baseY + dstY, 3, pixelState)) {
          renderer.drawPixel(baseX + dstX, baseY + dstY, pixelState);
        }
      }
    }
  }
}

template <TextRotation rotation = TextRotation::None>
static void renderCharImpl(const GfxRenderer& renderer, GfxRenderer::RenderMode renderMode,
                           const EpdFontFamily& fontFamily, const uint32_t cp, int cursorX, int cursorY,
                           const bool pixelState, const EpdFontFamily::Style style) {
  const EpdGlyph* glyph = fontFamily.getGlyph(cp, style);
  if (!glyph) {
    LOG_ERR("GFX", "No glyph for codepoint %d", cp);
    return;
  }

  const EpdFontData* fontData = fontFamily.getData(style);
  const bool is2Bit = fontData->is2Bit;
  const bool is8Bit = fontData->glyphBitmapBpp == 8;
  const uint8_t width = glyph->width;
  const uint8_t height = glyph->height;
  const int left = glyph->left;
  const int top = glyph->top;

  if constexpr (rotation == TextRotation::Rotated90CW) {
    const int ob = cursorX + fontData->ascender - top;
    const int ib = cursorY - left;
    if (!renderer.glyphIntersectsStrip(ob, ib - (width - 1), ob + height - 1, ib)) {
      return;
    }
  } else {
    const int gx0 = cursorX + left;
    const int gy0 = cursorY - top;
    if (!renderer.glyphIntersectsStrip(gx0, gy0, gx0 + width - 1, gy0 + height - 1)) {
      return;
    }
  }

  const uint8_t* bitmap = renderer.getGlyphBitmap(fontData, glyph);

  if (bitmap != nullptr) {
    int outerBase, innerBase;
    if constexpr (rotation == TextRotation::Rotated90CW) {
      outerBase = cursorX + fontData->ascender - top;  // screenX = outerBase + glyphY
      innerBase = cursorY - left;                      // screenY = innerBase - glyphX
    } else {
      outerBase = cursorY - top;   // screenY = outerBase + glyphY
      innerBase = cursorX + left;  // screenX = innerBase + glyphX
    }

    if (is8Bit) {
      int pixelPosition = 0;
      for (int glyphY = 0; glyphY < height; glyphY++) {
        const int outerCoord = outerBase + glyphY;
        for (int glyphX = 0; glyphX < width; glyphX++, pixelPosition++) {
          int screenX, screenY;
          if constexpr (rotation == TextRotation::Rotated90CW) {
            screenX = outerCoord;
            screenY = innerBase - glyphX;
          } else {
            screenX = innerBase + glyphX;
            screenY = outerCoord;
          }

          const uint8_t alpha = bitmap[pixelPosition];
          const uint8_t raw = quantiseCoverage(alpha);
          if (collectGlyphCoverage(renderer, screenX, screenY, raw, pixelState)) continue;
          if ((renderMode == GfxRenderer::BW && raw >= 2) || (renderMode == GfxRenderer::BW_GRAY_BASE && raw >= 2)) {
            renderer.drawPixel(screenX, screenY, pixelState);
          } else if (renderMode == GfxRenderer::GRAYSCALE_MSB && (raw == 1 || raw == 2)) {
            renderer.drawPixel(screenX, screenY, false);
          } else if (renderMode == GfxRenderer::GRAYSCALE_LSB && raw == 2) {
            renderer.drawPixel(screenX, screenY, false);
          } else if (renderMode == GfxRenderer::GRAYSCALE_BOTH && (raw == 1 || raw == 2)) {
            renderer.drawGrayPixel(screenX, screenY, raw == 2, true);
          }
        }
      }
    } else if (is2Bit) {
      int pixelPosition = 0;
      for (int glyphY = 0; glyphY < height; glyphY++) {
        const int outerCoord = outerBase + glyphY;
        for (int glyphX = 0; glyphX < width; glyphX++, pixelPosition++) {
          int screenX, screenY;
          if constexpr (rotation == TextRotation::Rotated90CW) {
            screenX = outerCoord;
            screenY = innerBase - glyphX;
          } else {
            screenX = innerBase + glyphX;
            screenY = outerCoord;
          }

          const uint8_t byte = bitmap[pixelPosition >> 2];
          const uint8_t bit_index = (3 - (pixelPosition & 3)) * 2;
          const uint8_t bmpVal = 3 - ((byte >> bit_index) & 0x3);
          if (collectGlyphCoverage(renderer, screenX, screenY, 3 - bmpVal, pixelState)) continue;

          if ((renderMode == GfxRenderer::BW && bmpVal < 3) ||
              (renderMode == GfxRenderer::BW_GRAY_BASE && bmpVal < 2)) {
            renderer.drawPixel(screenX, screenY, pixelState);
          } else if (renderMode == GfxRenderer::GRAYSCALE_MSB && (bmpVal == 1 || bmpVal == 2)) {
            renderer.drawPixel(screenX, screenY, false);
          } else if (renderMode == GfxRenderer::GRAYSCALE_LSB && bmpVal == 1) {
            renderer.drawPixel(screenX, screenY, false);
          } else if (renderMode == GfxRenderer::GRAYSCALE_BOTH && (bmpVal == 1 || bmpVal == 2)) {
            renderer.drawGrayPixel(screenX, screenY, bmpVal == 1, true);
          }
        }
      }
    } else {
      int pixelPosition = 0;
      for (int glyphY = 0; glyphY < height; glyphY++) {
        const int outerCoord = outerBase + glyphY;
        for (int glyphX = 0; glyphX < width; glyphX++, pixelPosition++) {
          int screenX, screenY;
          if constexpr (rotation == TextRotation::Rotated90CW) {
            screenX = outerCoord;
            screenY = innerBase - glyphX;
          } else {
            screenX = innerBase + glyphX;
            screenY = outerCoord;
          }

          const uint8_t byte = bitmap[pixelPosition >> 3];
          const uint8_t bit_index = 7 - (pixelPosition & 7);

          if (((byte >> bit_index) & 1) && !collectGlyphCoverage(renderer, screenX, screenY, 3, pixelState)) {
            renderer.drawPixel(screenX, screenY, pixelState);
          }
        }
      }
    }
  }
}

void GfxRenderer::drawPixel(const int x, const int y, const bool state) const {
  if (!getWriteTarget()) return;
  if (drawShade(x, y, state ? 3 : 0)) return;
  if (target_.clip && renderMode >= GRAYSCALE_LSB &&
      (x < target_.x0 || x >= target_.x1 || y < target_.clipY0 || y >= target_.y1)) {
    return;
  }
  int phyX = 0;
  int phyY = 0;

  rotateCoordinates(orientation, x, y, &phyX, &phyY, panelWidth, panelHeight);

  if (phyX < 0 || phyX >= panelWidth || phyY < 0 || phyY >= panelHeight) {
    LOG_ERR("GFX", "!! Outside range (%d, %d) -> (%d, %d)", x, y, phyX, phyY);
    return;
  }

  uint8_t* target = frameBuffer;
  uint32_t rowY = static_cast<uint32_t>(phyY);
  if (target_.strip) {
    if (phyY < target_.y0 || phyY >= target_.y0 + target_.rows) {
      return;  // pixel outside the band currently being rendered
    }
    target = target_.buffer;
    rowY = static_cast<uint32_t>(phyY - target_.y0);
  }

  const uint32_t byteIndex = rowY * panelWidthBytes + (phyX / 8);
  const uint8_t bitPosition = 7 - (phyX % 8);  // MSB first

  if (state) {
    target[byteIndex] &= ~(1 << bitPosition);  // Clear bit
  } else {
    target[byteIndex] |= 1 << bitPosition;  // Set bit
  }
}

bool GfxRenderer::drawCoverage(const int x, const int y, const uint8_t coverage, const bool blackInk) const {
  if (target_.coverage != CoveragePolicy::Collect) return false;
  const GrayFrame tuple = activeTuple();
  if (!tuple.valid() || accountCancellation() || isFontCacheScanning()) return false;
  if (target_.owner == FrameOwner::LiveUi ? !liveCoherent_ : !*target_.coherent) return false;
  if (target_.clip && (x < target_.x0 || x >= target_.x1 || y < target_.clipY0 || y >= target_.y1)) return false;
  int px, py;
  rotateCoordinates(orientation, x, y, &px, &py, panelWidth, panelHeight);
  return tuple.writeCoverage(px, py, coverage, blackInk);
}

void GfxRenderer::drawGrayPixel(const int x, const int y, const bool lsb, const bool msb) const {
  if (!getWriteTarget()) return;
  if ((!lsb && !msb) || !target_.strip || !target_.secondary) return;
  if (target_.clip && (x < target_.x0 || x >= target_.x1 || y < target_.clipY0 || y >= target_.y1)) {
    return;
  }

  int phyX = 0;
  int phyY = 0;
  rotateCoordinates(orientation, x, y, &phyX, &phyY, panelWidth, panelHeight);
  if (phyX < 0 || phyX >= panelWidth || phyY < target_.y0 || phyY >= target_.y0 + target_.rows) return;

  const uint32_t byteIndex = static_cast<uint32_t>(phyY - target_.y0) * panelWidthBytes + (phyX >> 3);
  const uint8_t bitMask = static_cast<uint8_t>(1u << (7 - (phyX & 7)));
  if (lsb) target_.buffer[byteIndex] |= bitMask;
  if (msb) target_.secondary[byteIndex] |= bitMask;
}

int GfxRenderer::getTextWidth(const int fontId, const char* text, const EpdFontFamily::Style style,
                              const BidiUtils::BidiBaseDir baseDir) const {
  if (text == nullptr || *text == '\0') {
    return 0;
  }

  const int resolvedFontId = resolveTextFontId(fontId, text, style);
  const auto fontIt = fontMap.find(resolvedFontId);
  if (fontIt == fontMap.end()) {
    LOG_ERR("GFX", "Font %d not found", resolvedFontId);
    return 0;
  }

  std::string visual;
  const char* renderedText = resolveVisualText(text, visual, baseDir);

  int w = 0, h = 0;
  fontIt->second.getTextDimensions(renderedText, &w, &h, style);
  return w;
}

void GfxRenderer::drawCenteredText(const int fontId, const int y, const char* text, const bool black,
                                   const EpdFontFamily::Style style, const BidiUtils::BidiBaseDir baseDir) const {
  const int x = (getScreenWidth() - getTextWidth(fontId, text, style, baseDir)) / 2;
  drawText(fontId, x, y, text, black, style, baseDir);
}

void GfxRenderer::drawText(const int fontId, const int x, const int y, const char* text, const bool black,
                           const EpdFontFamily::Style style, const BidiUtils::BidiBaseDir baseDir) const {
  if (text == nullptr || *text == '\0') {
    return;
  }

  const int resolvedFontId = resolveTextFontId(fontId, text, style);

  std::string visual;
  const char* renderedText = resolveVisualText(text, visual, baseDir);

  const int yPos = y + getFontAscenderSize(resolvedFontId);
  int lastBaseX = x;
  int lastBaseLeft = 0;
  int lastBaseWidth = 0;
  int lastBaseTop = 0;
  int32_t prevAdvanceFP = 0;  // 12.4 fixed-point: prev glyph's advance + next kern for snap

  if (fontCacheManager_ && fontCacheManager_->isScanning()) {
    fontCacheManager_->recordText(renderedText, resolvedFontId, style);
    return;
  }

  const auto fontIt = fontMap.find(resolvedFontId);
  if (fontIt == fontMap.end()) {
    LOG_ERR("GFX", "Font %d not found", resolvedFontId);
    return;
  }
  const auto& font = fontIt->second;

  const char* textCursor = renderedText;
  uint32_t cp;
  uint32_t prevCp = 0;
  while ((cp = utf8NextCodepoint(reinterpret_cast<const uint8_t**>(&textCursor)))) {
    if (utf8IsCombiningMark(cp) || BidiUtils::isTransparentMark(cp)) {
      const EpdGlyph* combiningGlyph = font.getGlyph(cp, style);
      if (!combiningGlyph) continue;
      const auto anchor = combiningMark::anchorFor(cp);
      const int raiseBy =
          combiningMark::raiseAboveBase(anchor, combiningGlyph->top, combiningGlyph->height, lastBaseTop);
      const int combiningX = combiningMark::anchorOver(anchor, lastBaseX, lastBaseLeft, lastBaseWidth,
                                                       combiningGlyph->left, combiningGlyph->width);
      renderCharImpl<TextRotation::None>(*this, renderMode, font, cp, combiningX, yPos - raiseBy, black, style);
      continue;
    }

    cp = font.applyLigatures(cp, textCursor, style);

    if (prevCp != 0) {
      const auto kernFP = font.getKerning(prevCp, cp, style);  // 4.4 fixed-point kern
      lastBaseX += fp4::toPixel(prevAdvanceFP + kernFP);       // snap 12.4 fixed-point to nearest pixel
    }

    const EpdGlyph* glyph = font.getGlyph(cp, style);

    lastBaseLeft = glyph ? glyph->left : 0;
    lastBaseWidth = glyph ? glyph->width : 0;
    lastBaseTop = glyph ? glyph->top : 0;
    prevAdvanceFP = glyph ? glyph->advanceX : 0;  // 12.4 fixed-point

    const bool isSupSub = (style & (EpdFontFamily::SUP | EpdFontFamily::SUB)) != 0;
    if (isSupSub) {
      prevAdvanceFP = (prevAdvanceFP + 1) / 2;
    }

    if (isSupSub) {
      renderCharScaled(*this, renderMode, font, cp, lastBaseX, yPos, black, style);
    } else {
      renderCharImpl<TextRotation::None>(*this, renderMode, font, cp, lastBaseX, yPos, black, style);
    }
    prevCp = cp;
  }
}

namespace {
const char* resolveVisualText(const char* text, std::string& visualBuffer, const BidiUtils::BidiBaseDir baseDir) {
  if (!text || *text == '\0') return text;

  if (baseDir != BidiUtils::BidiBaseDir::RTL) {
    bool hasRtlBytes = false;
    for (const unsigned char* q = reinterpret_cast<const unsigned char*>(text); *q; ++q) {
      if (*q >= 0xD6 && *q <= 0xDB) {
        hasRtlBytes = true;
        break;
      }
    }
    if (!hasRtlBytes) return text;
  }

  if (BidiUtils::applyBidiVisual(text, visualBuffer, static_cast<int>(baseDir)) && !visualBuffer.empty()) {
    return visualBuffer.c_str();
  }
  return text;
}
}  // namespace

void GfxRenderer::drawLine(int x1, int y1, int x2, int y2, const bool state) const {
  if (fontCacheManager_ && fontCacheManager_->isScanning()) return;
  if (x1 == x2) {
    if (y2 < y1) {
      std::swap(y1, y2);
    }
    for (int y = y1; y <= y2; y++) {
      drawPixel(x1, y, state);
    }
  } else if (y1 == y2) {
    if (x2 < x1) {
      std::swap(x1, x2);
    }
    for (int x = x1; x <= x2; x++) {
      drawPixel(x, y1, state);
    }
  } else {
    int dx = x2 - x1;
    int dy = y2 - y1;
    int sx = (dx > 0) ? 1 : -1;
    int sy = (dy > 0) ? 1 : -1;
    dx = sx * dx;  // abs
    dy = sy * dy;  // abs

    int err = dx - dy;
    while (true) {
      drawPixel(x1, y1, state);
      if (x1 == x2 && y1 == y2) break;
      int e2 = 2 * err;
      if (e2 > -dy) {
        err -= dy;
        x1 += sx;
      }
      if (e2 < dx) {
        err += dx;
        y1 += sy;
      }
    }
  }
}

void GfxRenderer::drawLine(int x1, int y1, int x2, int y2, const int lineWidth, const bool state) const {
  for (int i = 0; i < lineWidth; i++) {
    drawLine(x1, y1 + i, x2, y2 + i, state);
  }
}

void GfxRenderer::drawRect(const int x, const int y, const int width, const int height, const bool state) const {
  drawLine(x, y, x + width - 1, y, state);
  drawLine(x + width - 1, y, x + width - 1, y + height - 1, state);
  drawLine(x + width - 1, y + height - 1, x, y + height - 1, state);
  drawLine(x, y, x, y + height - 1, state);
}

void GfxRenderer::drawRect(const int x, const int y, const int width, const int height, const int lineWidth,
                           const bool state) const {
  for (int i = 0; i < lineWidth; i++) {
    drawLine(x + i, y + i, x + width - i, y + i, state);
    drawLine(x + width - i, y + i, x + width - i, y + height - i, state);
    drawLine(x + width - i, y + height - i, x + i, y + height - i, state);
    drawLine(x + i, y + height - i, x + i, y + i, state);
  }
}

void GfxRenderer::drawArc(const int maxRadius, const int cx, const int cy, const int xDir, const int yDir,
                          const int lineWidth, const bool state) const {
  const int stroke = std::min(lineWidth, maxRadius);
  const int innerRadius = std::max(maxRadius - stroke, 0);
  const int outerRadius = maxRadius;

  if (outerRadius <= 0) {
    return;
  }

  const int outerRadiusSq = outerRadius * outerRadius;
  const int innerRadiusSq = innerRadius * innerRadius;

  int xOuter = outerRadius;
  int xInner = innerRadius;

  for (int dy = 0; dy <= outerRadius; ++dy) {
    while (xOuter > 0 && (xOuter * xOuter + dy * dy) > outerRadiusSq) {
      --xOuter;
    }
    while (xInner > 0 && ((xInner - 1) * (xInner - 1) + dy * dy) >= innerRadiusSq) {
      --xInner;
    }

    if (xOuter < xInner) {
      continue;
    }

    const int x0 = cx + xDir * xInner;
    const int x1 = cx + xDir * xOuter;
    const int left = std::min(x0, x1);
    const int width = std::abs(x1 - x0) + 1;
    const int py = cy + yDir * dy;

    if (width > 0) {
      fillRect(left, py, width, 1, state);
    }
  }
};

void GfxRenderer::drawRoundedRect(const int x, const int y, const int width, const int height, const int lineWidth,
                                  const int cornerRadius, bool state) const {
  drawRoundedRect(x, y, width, height, lineWidth, cornerRadius, true, true, true, true, state);
}

void GfxRenderer::drawRoundedRect(const int x, const int y, const int width, const int height, const int lineWidth,
                                  const int cornerRadius, bool roundTopLeft, bool roundTopRight, bool roundBottomLeft,
                                  bool roundBottomRight, bool state) const {
  if (lineWidth <= 0 || width <= 0 || height <= 0) {
    return;
  }

  const int maxRadius = std::min({cornerRadius, width / 2, height / 2});
  if (maxRadius <= 0) {
    drawRect(x, y, width, height, lineWidth, state);
    return;
  }

  const int stroke = std::min(lineWidth, maxRadius);
  const int right = x + width - 1;
  const int bottom = y + height - 1;

  const int horizontalWidth = width - 2 * maxRadius;
  if (horizontalWidth > 0) {
    if (roundTopLeft || roundTopRight) {
      fillRect(x + maxRadius, y, horizontalWidth, stroke, state);
    }
    if (roundBottomLeft || roundBottomRight) {
      fillRect(x + maxRadius, bottom - stroke + 1, horizontalWidth, stroke, state);
    }
  }

  const int verticalHeight = height - 2 * maxRadius;
  if (verticalHeight > 0) {
    if (roundTopLeft || roundBottomLeft) {
      fillRect(x, y + maxRadius, stroke, verticalHeight, state);
    }
    if (roundTopRight || roundBottomRight) {
      fillRect(right - stroke + 1, y + maxRadius, stroke, verticalHeight, state);
    }
  }

  if (roundTopLeft) {
    drawArc(maxRadius, x + maxRadius, y + maxRadius, -1, -1, lineWidth, state);
  }
  if (roundTopRight) {
    drawArc(maxRadius, right - maxRadius, y + maxRadius, 1, -1, lineWidth, state);
  }
  if (roundBottomRight) {
    drawArc(maxRadius, right - maxRadius, bottom - maxRadius, 1, 1, lineWidth, state);
  }
  if (roundBottomLeft) {
    drawArc(maxRadius, x + maxRadius, bottom - maxRadius, -1, 1, lineWidth, state);
  }
}

void GfxRenderer::fillRect(const int x, const int y, const int width, const int height, const bool state) const {
  if (state) {
    fillRectImpl<Color::Black>(x, y, width, height);
  } else {
    fillRectImpl<Color::White>(x, y, width, height);
  }
}

template <>
void GfxRenderer::drawPixelDither<Color::Clear>(const int x, const int y) const {}

template <>
void GfxRenderer::drawPixelDither<Color::Black>(const int x, const int y) const {
  drawPixel(x, y, true);
}

template <>
void GfxRenderer::drawPixelDither<Color::White>(const int x, const int y) const {
  drawPixel(x, y, false);
}

template <>
void GfxRenderer::drawPixelDither<Color::LightGray>(const int x, const int y) const {
  drawPixel(x, y, x % 2 == 0 && y % 2 == 0);
}

template <>
void GfxRenderer::drawPixelDither<Color::DarkGray>(const int x, const int y) const {
  drawPixel(x, y, (x + y) % 2 == 0);  // TODO: maybe find a better pattern?
}

void GfxRenderer::fillRectDither(const int x, const int y, const int width, const int height, Color color) const {
  switch (color) {
    case Color::Clear:
      break;
    case Color::Black:
      fillRectImpl<Color::Black>(x, y, width, height);
      break;
    case Color::White:
      fillRectImpl<Color::White>(x, y, width, height);
      break;
    case Color::LightGray:
      fillRectImpl<Color::LightGray>(x, y, width, height);
      break;
    case Color::DarkGray:
      fillRectImpl<Color::DarkGray>(x, y, width, height);
      break;
  }
}

template <Color C>
void GfxRenderer::fillRectImpl(const int x, const int y, const int width, const int height) const {
  if (!getWriteTarget()) return;
  if constexpr (C == Color::Clear) return;
  if (width <= 0 || height <= 0) return;
  if (fontCacheManager_ && fontCacheManager_->isScanning()) return;

  const int screenW = getScreenWidth();
  const int screenH = getScreenHeight();
  int lx0 = std::max(0, x);
  int ly0 = std::max(0, y);
  int lx1 = std::min(screenW, x + width);
  int ly1 = std::min(screenH, y + height);
  const GrayFrame tuple = activeTuple();
  if (target_.clip && (tuple.valid() || renderMode >= GRAYSCALE_LSB)) {
    lx0 = std::max(lx0, target_.x0);
    ly0 = std::max(ly0, target_.clipY0);
    lx1 = std::min(lx1, target_.x1);
    ly1 = std::min(ly1, target_.y1);
  }
  if (lx0 >= lx1 || ly0 >= ly1) return;

  int paX, paY, pbX, pbY;
  rotateCoordinates(orientation, lx0, ly0, &paX, &paY, panelWidth, panelHeight);
  rotateCoordinates(orientation, lx1 - 1, ly1 - 1, &pbX, &pbY, panelWidth, panelHeight);

  const int phyX0 = std::min(paX, pbX);
  const int phyX1 = std::max(paX, pbX);  // inclusive
  int phyY0 = std::min(paY, pbY);
  int phyY1 = std::max(paY, pbY);

  uint8_t* target = getWriteTarget();
  const int originY = getWriteOriginY();
  const int writeRows = getWriteRows();
  phyY0 = std::max(phyY0, originY);
  phyY1 = std::min(phyY1, originY + writeRows - 1);
  if (phyY0 > phyY1) return;

  if (tuple.valid()) {
    for (int py = phyY0; py <= phyY1; ++py) tuple.clearSelectors(phyX0, phyX1 + 1, py);
  }

  const int byteStart = phyX0 >> 3;
  const int byteEnd = phyX1 >> 3;  // inclusive
  const uint8_t headMask = static_cast<uint8_t>(0xFFu >> (phyX0 & 7));
  const uint8_t tailMask = static_cast<uint8_t>(0xFFu << (7 - (phyX1 & 7)));
  const int32_t panelStride = static_cast<int32_t>(panelWidthBytes);

  if constexpr (C == Color::Black || C == Color::White) {
    const uint8_t fillByte = (C == Color::Black) ? 0x00u : 0xFFu;
    for (int py = phyY0; py <= phyY1; ++py) {
      uint8_t* row = target + static_cast<int32_t>(py - originY) * panelStride;
      if (byteStart == byteEnd) {
        const uint8_t mask = headMask & tailMask;
        if constexpr (C == Color::Black) {
          row[byteStart] &= static_cast<uint8_t>(~mask);
        } else {
          row[byteStart] |= mask;
        }
      } else {
        if constexpr (C == Color::Black) {
          row[byteStart] &= static_cast<uint8_t>(~headMask);
          if (byteEnd > byteStart + 1) {
            memset(row + byteStart + 1, fillByte, byteEnd - byteStart - 1);
          }
          row[byteEnd] &= static_cast<uint8_t>(~tailMask);
        } else {
          row[byteStart] |= headMask;
          if (byteEnd > byteStart + 1) {
            memset(row + byteStart + 1, fillByte, byteEnd - byteStart - 1);
          }
          row[byteEnd] |= tailMask;
        }
      }
    }
  } else {
    int dlxPerPhyX = 0, dlyPerPhyX = 0;
    switch (orientation) {
      case Portrait:
        dlxPerPhyX = 0;
        dlyPerPhyX = 1;
        break;
      case PortraitInverted:
        dlxPerPhyX = 0;
        dlyPerPhyX = -1;
        break;
      case LandscapeClockwise:
        dlxPerPhyX = -1;
        dlyPerPhyX = 0;
        break;
      case LandscapeCounterClockwise:
        dlxPerPhyX = 1;
        dlyPerPhyX = 0;
        break;
    }

    uint8_t blackMasks[2];
    for (int parityIdx = 0; parityIdx < 2; ++parityIdx) {
      const int samplePy = phyY0 + parityIdx;
      int lxBase = 0, lyBase = 0;
      switch (orientation) {
        case Portrait:
          lxBase = panelHeight - 1 - samplePy;
          lyBase = byteStart * 8;
          break;
        case PortraitInverted:
          lxBase = samplePy;
          lyBase = panelWidth - 1 - byteStart * 8;
          break;
        case LandscapeClockwise:
          lxBase = panelWidth - 1 - byteStart * 8;
          lyBase = panelHeight - 1 - samplePy;
          break;
        case LandscapeCounterClockwise:
          lxBase = byteStart * 8;
          lyBase = samplePy;
          break;
      }
      uint8_t mask = 0;
      for (int b = 0; b < 8; ++b) {
        const int lx = lxBase + b * dlxPerPhyX;
        const int ly = lyBase + b * dlyPerPhyX;
        bool isBlack;
        if constexpr (C == Color::LightGray) {
          isBlack = ((lx & 1) == 0) && ((ly & 1) == 0);
        } else {  // DarkGray
          isBlack = (((lx + ly) & 1) == 0);
        }
        if (isBlack) mask |= static_cast<uint8_t>(1u << (7 - b));
      }
      blackMasks[samplePy & 1] = mask;
    }

    for (int py = phyY0; py <= phyY1; ++py) {
      const uint8_t blackMask = blackMasks[py & 1];
      const uint8_t whiteMask = static_cast<uint8_t>(~blackMask);

      uint8_t* row = target + static_cast<int32_t>(py - originY) * panelStride;
      if (byteStart == byteEnd) {
        const uint8_t rectMask = headMask & tailMask;
        row[byteStart] = static_cast<uint8_t>((row[byteStart] & ~rectMask) | (rectMask & whiteMask));
      } else {
        row[byteStart] = static_cast<uint8_t>((row[byteStart] & ~headMask) | (headMask & whiteMask));
        if (byteEnd > byteStart + 1) {
          memset(row + byteStart + 1, whiteMask, byteEnd - byteStart - 1);
        }
        row[byteEnd] = static_cast<uint8_t>((row[byteEnd] & ~tailMask) | (tailMask & whiteMask));
      }
    }
  }
}

template void GfxRenderer::fillRectImpl<Color::Black>(int, int, int, int) const;
template void GfxRenderer::fillRectImpl<Color::White>(int, int, int, int) const;
template void GfxRenderer::fillRectImpl<Color::LightGray>(int, int, int, int) const;
template void GfxRenderer::fillRectImpl<Color::DarkGray>(int, int, int, int) const;

void GfxRenderer::maskRoundedRectOutsideCorners(const int x, const int y, const int width, const int height,
                                                const int radius, const Color color) const {
  if (radius <= 0 || color == Color::Clear) {
    return;
  }

  const int rr = radius - 1;
  const int rr2 = rr * rr;
  for (int dy = 0; dy < radius; dy++) {
    for (int dx = 0; dx < radius; dx++) {
      const int tx = rr - dx;
      const int ty = rr - dy;
      if (tx * tx + ty * ty > rr2) {
        if (color == Color::White || color == Color::Black) {
          bool state = color == Color::Black;
          drawPixel(x + dx, y + dy, state);                           // top-left
          drawPixel(x + width - 1 - dx, y + dy, state);               // top-right
          drawPixel(x + dx, y + height - 1 - dy, state);              // bottom-left
          drawPixel(x + width - 1 - dx, y + height - 1 - dy, state);  // bottom-right
        } else if (color == Color::LightGray) {
          drawPixelDither<Color::LightGray>(x + dx, y + dy);                           // top-left
          drawPixelDither<Color::LightGray>(x + width - 1 - dx, y + dy);               // top-right
          drawPixelDither<Color::LightGray>(x + dx, y + height - 1 - dy);              // bottom-left
          drawPixelDither<Color::LightGray>(x + width - 1 - dx, y + height - 1 - dy);  // bottom-right
        } else if (color == Color::DarkGray) {
          drawPixelDither<Color::DarkGray>(x + dx, y + dy);                           // top-left
          drawPixelDither<Color::DarkGray>(x + width - 1 - dx, y + dy);               // top-right
          drawPixelDither<Color::DarkGray>(x + dx, y + height - 1 - dy);              // bottom-left
          drawPixelDither<Color::DarkGray>(x + width - 1 - dx, y + height - 1 - dy);  // bottom-right
        }
      }
    }
  }
}

template <Color color>
void GfxRenderer::fillArc(const int maxRadius, const int cx, const int cy, const int xDir, const int yDir) const {
  if (maxRadius <= 0) return;

  if constexpr (color == Color::Clear) {
    return;
  }

  const int radiusSq = maxRadius * maxRadius;

  int x = maxRadius;
  for (int dy = 0; dy <= maxRadius; ++dy) {
    while (x > 0 && (x * x + dy * dy) > radiusSq) {
      --x;
    }
    if (x < 0) break;

    const int py = cy + yDir * dy;
    if (py < 0 || py >= getScreenHeight()) continue;

    int x0 = cx;
    int x1 = cx + xDir * x;
    if (x0 > x1) std::swap(x0, x1);
    const int width = x1 - x0 + 1;

    if (width <= 0) continue;

    if constexpr (color == Color::Black) {
      fillRect(x0, py, width, 1, true);
    } else if constexpr (color == Color::White) {
      fillRect(x0, py, width, 1, false);
    } else {
      fillRectDither(x0, py, width, 1, color);
    }
  }
}

void GfxRenderer::fillRoundedRect(const int x, const int y, const int width, const int height, const int cornerRadius,
                                  const Color color) const {
  fillRoundedRect(x, y, width, height, cornerRadius, true, true, true, true, color);
}

void GfxRenderer::fillRoundedRect(const int x, const int y, const int width, const int height, const int cornerRadius,
                                  bool roundTopLeft, bool roundTopRight, bool roundBottomLeft, bool roundBottomRight,
                                  const Color color) const {
  if (width <= 0 || height <= 0) {
    return;
  }

  const int roundedSides = (!roundTopLeft || !roundTopRight || !roundBottomLeft || !roundBottomRight) ? 1 : 2;
  const int maxRadius = std::min({cornerRadius, width / roundedSides, height / roundedSides});
  if (maxRadius <= 0) {
    fillRectDither(x, y, width, height, color);
    return;
  }

  const int horizontalWidth = width - 2 * maxRadius;
  if (horizontalWidth > 0) {
    fillRectDither(x + maxRadius + 1, y, horizontalWidth - 2, height, color);
  }

  const int leftFillTop = y + (roundTopLeft ? (maxRadius + 1) : 0);
  const int leftFillBottom = y + height - 1 - (roundBottomLeft ? (maxRadius + 1) : 0);
  if (leftFillBottom >= leftFillTop) {
    fillRectDither(x, leftFillTop, maxRadius + 1, leftFillBottom - leftFillTop + 1, color);
  }

  const int rightFillTop = y + (roundTopRight ? (maxRadius + 1) : 0);
  const int rightFillBottom = y + height - 1 - (roundBottomRight ? (maxRadius + 1) : 0);
  if (rightFillBottom >= rightFillTop) {
    fillRectDither(x + width - maxRadius - 1, rightFillTop, maxRadius + 1, rightFillBottom - rightFillTop + 1, color);
  }

  auto fillArcTemplated = [this](int maxRadius, int cx, int cy, int xDir, int yDir, Color color) {
    switch (color) {
      case Color::Clear:
        break;
      case Color::Black:
        fillArc<Color::Black>(maxRadius, cx, cy, xDir, yDir);
        break;
      case Color::White:
        fillArc<Color::White>(maxRadius, cx, cy, xDir, yDir);
        break;
      case Color::LightGray:
        fillArc<Color::LightGray>(maxRadius, cx, cy, xDir, yDir);
        break;
      case Color::DarkGray:
        fillArc<Color::DarkGray>(maxRadius, cx, cy, xDir, yDir);
        break;
    }
  };

  if (roundTopLeft) {
    fillArcTemplated(maxRadius, x + maxRadius, y + maxRadius, -1, -1, color);
  }

  if (roundTopRight) {
    fillArcTemplated(maxRadius, x + width - maxRadius - 1, y + maxRadius, 1, -1, color);
  }

  if (roundBottomRight) {
    fillArcTemplated(maxRadius, x + width - maxRadius - 1, y + height - maxRadius - 1, 1, 1, color);
  }

  if (roundBottomLeft) {
    fillArcTemplated(maxRadius, x + maxRadius, y + height - maxRadius - 1, -1, 1, color);
  }
}

void GfxRenderer::drawImage(const uint8_t bitmap[], const int x, const int y, const int width, const int height) const {
  if (!getWriteTarget() || !bitmap || width <= 0 || height <= 0) return;
  int rotatedX = 0;
  int rotatedY = 0;
  rotateCoordinates(orientation, x, y, &rotatedX, &rotatedY, panelWidth, panelHeight);
  switch (orientation) {
    case Portrait:
      rotatedY = rotatedY - height;
      break;
    case PortraitInverted:
      rotatedX = rotatedX - width;
      break;
    case LandscapeClockwise:
      rotatedY = rotatedY - height;
      rotatedX = rotatedX - width;
      break;
    case LandscapeCounterClockwise:
      break;
  }
  drawPhysicalImage(bitmap, rotatedX, rotatedY, width, height);
}

void GfxRenderer::drawPhysicalImage(const uint8_t* bitmap, uint16_t x, uint16_t y, uint16_t width,
                                    uint16_t height) const {
  const int sourceByteX = x / 8;
  const int sourceStride = width / 8;
  uint8_t* target = getWriteTarget();
  const int originY = getWriteOriginY();
  int x0 = sourceByteX * 8;
  int x1 = std::min<int>(panelWidth, x0 + sourceStride * 8);
  int y0 = std::max<int>(y, originY);
  int y1 = std::min<int>(y + height, originY + getWriteRows());
  if (target_.clip) {
    int ax, ay, bx, by;
    rotateCoordinates(orientation, target_.x0, target_.clipY0, &ax, &ay, panelWidth, panelHeight);
    rotateCoordinates(orientation, target_.x1 - 1, target_.y1 - 1, &bx, &by, panelWidth, panelHeight);
    x0 = std::max(x0, std::min(ax, bx));
    x1 = std::min(x1, std::max(ax, bx) + 1);
    y0 = std::max(y0, std::min(ay, by));
    y1 = std::min(y1, std::max(ay, by) + 1);
  }
  if (x0 >= x1 || y0 >= y1) return;
  const GrayFrame tuple = activeTuple();
  const bool hasTuple = tuple.valid();
  for (int row = y0; row < y1; ++row) {
    uint8_t* destination = target + (row - originY) * panelWidthBytes;
    GrayFrame::copyPlaneRow(destination, bitmap + (row - y) * sourceStride, x0, x1, 0, sourceByteX);
    if (hasTuple) tuple.clearSelectors(x0, x1, row);
  }
}

bool GfxRenderer::drawShade(const int x, const int y, const uint8_t darkness) const {
  const GrayFrame tuple = activeTuple();
  if (!tuple.valid()) return false;
  if (target_.clip && (x < target_.x0 || x >= target_.x1 || y < target_.clipY0 || y >= target_.y1)) return true;
  int px, py;
  rotateCoordinates(orientation, x, y, &px, &py, panelWidth, panelHeight);
  tuple.writeShade(px, py, darkness);
  return true;
}

void GfxRenderer::drawBitmapPixel(const int x, const int y, const uint8_t value) const {
  if (value >= 3 || drawShade(x, y, 3 - value)) return;
  if (renderMode == GRAYSCALE_BOTH) {
    drawGrayPixel(x, y, value == 1, value > 0);
    return;
  }
  constexpr uint8_t paintedValues[] = {0x07, 0x03, 0x02, 0x06};
  if (renderMode >= BW && renderMode <= GRAYSCALE_MSB && (paintedValues[renderMode] & (1 << value))) {
    drawPixel(x, y, renderMode < GRAYSCALE_LSB);
  }
}

void GfxRenderer::drawIcon(const uint8_t bitmap[], const int x, const int y, const int size) const {
  const int rowBytes = (size + 7) / 8;
  for (int row = 0; row < size; row++) {
    for (int col = 0; col < size; col++) {
      const uint8_t byte = bitmap[row * rowBytes + (col >> 3)];
      const bool ink = ((byte >> (7 - (col & 7))) & 1) == 0;
      if (ink) {
        drawPixel(x + (size - 1 - row), y + col, true);
      }
    }
  }
}

void GfxRenderer::drawBitmap(const Bitmap& bitmap, const int x, const int y, const int maxWidth, const int maxHeight,
                             const float cropX, const float cropY) const {
  if (fontCacheManager_ && fontCacheManager_->isScanning()) return;
  if (bitmap.is1Bit() && cropX == 0.0f && cropY == 0.0f) {
    drawBitmap1Bit(bitmap, x, y, maxWidth, maxHeight);
    return;
  }

  float scale = 1.0f;
  bool isScaled = false;
  int cropPixX = std::floor(bitmap.getWidth() * cropX / 2.0f);
  int cropPixY = std::floor(bitmap.getHeight() * cropY / 2.0f);
  LOG_DBG("GFX", "Cropping %dx%d by %dx%d pix, is %s", bitmap.getWidth(), bitmap.getHeight(), cropPixX, cropPixY,
          bitmap.isTopDown() ? "top-down" : "bottom-up");

  const float croppedWidth = (1.0f - cropX) * static_cast<float>(bitmap.getWidth());
  const float croppedHeight = (1.0f - cropY) * static_cast<float>(bitmap.getHeight());
  bool hasTargetBounds = false;
  float fitScale = 1.0f;

  if (maxWidth > 0 && croppedWidth > 0.0f) {
    fitScale = static_cast<float>(maxWidth) / croppedWidth;
    hasTargetBounds = true;
  }

  if (maxHeight > 0 && croppedHeight > 0.0f) {
    const float heightScale = static_cast<float>(maxHeight) / croppedHeight;
    fitScale = hasTargetBounds ? std::min(fitScale, heightScale) : heightScale;
    hasTargetBounds = true;
  }

  if (hasTargetBounds && fitScale < 1.0f) {
    scale = fitScale;
    isScaled = true;
  }
  LOG_DBG("GFX", "Scaling by %f - %s", scale, isScaled ? "scaled" : "not scaled");

  const int outputRowSize = (bitmap.getWidth() + 3) / 4;
  auto* outputRow = static_cast<uint8_t*>(malloc(outputRowSize));
  auto* rowBytes = static_cast<uint8_t*>(malloc(bitmap.getRowBytes()));

  if (!outputRow || !rowBytes) {
    LOG_ERR("GFX", "!! Failed to allocate BMP row buffers");
    free(outputRow);
    free(rowBytes);
    return;
  }

  for (int bmpY = 0; bmpY < (bitmap.getHeight() - cropPixY); bmpY++) {
    int screenY = -cropPixY + (bitmap.isTopDown() ? bmpY : bitmap.getHeight() - 1 - bmpY);
    if (isScaled) {
      screenY = std::floor(screenY * scale);
    }
    screenY += y;  // the offset should not be scaled
    if (screenY >= getScreenHeight()) {
      break;
    }

    if (bitmap.readNextRow(outputRow, rowBytes) != BmpReaderError::Ok) {
      LOG_ERR("GFX", "Failed to read row %d from bitmap", bmpY);
      free(outputRow);
      free(rowBytes);
      return;
    }

    if (screenY < 0) {
      continue;
    }

    if (bmpY < cropPixY) {
      continue;
    }

    for (int bmpX = cropPixX; bmpX < bitmap.getWidth() - cropPixX; bmpX++) {
      int screenX = bmpX - cropPixX;
      if (isScaled) {
        screenX = std::floor(screenX * scale);
      }
      screenX += x;  // the offset should not be scaled
      if (screenX >= getScreenWidth()) {
        break;
      }
      if (screenX < 0) {
        continue;
      }

      const uint8_t val = outputRow[bmpX / 4] >> (6 - ((bmpX * 2) % 8)) & 0x3;

      drawBitmapPixel(screenX, screenY, val);
    }
  }

  free(outputRow);
  free(rowBytes);
}

void GfxRenderer::drawBitmap1Bit(const Bitmap& bitmap, const int x, const int y, const int maxWidth,
                                 const int maxHeight) const {
  float scale = 1.0f;
  bool isScaled = false;
  if (maxWidth > 0 && bitmap.getWidth() > maxWidth) {
    scale = static_cast<float>(maxWidth) / static_cast<float>(bitmap.getWidth());
    isScaled = true;
  }
  if (maxHeight > 0 && bitmap.getHeight() > maxHeight) {
    scale = std::min(scale, static_cast<float>(maxHeight) / static_cast<float>(bitmap.getHeight()));
    isScaled = true;
  }

  const int outputRowSize = (bitmap.getWidth() + 3) / 4;
  auto* outputRow = static_cast<uint8_t*>(malloc(outputRowSize));
  auto* rowBytes = static_cast<uint8_t*>(malloc(bitmap.getRowBytes()));

  if (!outputRow || !rowBytes) {
    LOG_ERR("GFX", "!! Failed to allocate 1-bit BMP row buffers");
    free(outputRow);
    free(rowBytes);
    return;
  }

  for (int bmpY = 0; bmpY < bitmap.getHeight(); bmpY++) {
    if (bitmap.readNextRow(outputRow, rowBytes) != BmpReaderError::Ok) {
      LOG_ERR("GFX", "Failed to read row %d from 1-bit bitmap", bmpY);
      free(outputRow);
      free(rowBytes);
      return;
    }

    const int bmpYOffset = bitmap.isTopDown() ? bmpY : bitmap.getHeight() - 1 - bmpY;
    int screenY = y + (isScaled ? static_cast<int>(std::floor(bmpYOffset * scale)) : bmpYOffset);
    if (screenY >= getScreenHeight()) {
      continue;  // Continue reading to keep row counter in sync
    }
    if (screenY < 0) {
      continue;
    }

    for (int bmpX = 0; bmpX < bitmap.getWidth(); bmpX++) {
      int screenX = x + (isScaled ? static_cast<int>(std::floor(bmpX * scale)) : bmpX);
      if (screenX >= getScreenWidth()) {
        break;
      }
      if (screenX < 0) {
        continue;
      }

      const uint8_t val = outputRow[bmpX / 4] >> (6 - ((bmpX * 2) % 8)) & 0x3;

      if (val < 3) {
        drawPixel(screenX, screenY, true);
      }
    }
  }

  free(outputRow);
  free(rowBytes);
}

void GfxRenderer::fillPolygon(const int* xPoints, const int* yPoints, int numPoints, bool state) const {
  if (numPoints < 3) return;

  int minY = yPoints[0], maxY = yPoints[0];
  for (int i = 1; i < numPoints; i++) {
    if (yPoints[i] < minY) minY = yPoints[i];
    if (yPoints[i] > maxY) maxY = yPoints[i];
  }

  if (minY < 0) minY = 0;
  if (maxY >= getScreenHeight()) maxY = getScreenHeight() - 1;

  auto* nodeX = static_cast<int*>(malloc(numPoints * sizeof(int)));
  if (!nodeX) {
    LOG_ERR("GFX", "!! Failed to allocate polygon node buffer");
    return;
  }

  for (int scanY = minY; scanY <= maxY; scanY++) {
    int nodes = 0;

    int j = numPoints - 1;
    for (int i = 0; i < numPoints; i++) {
      if ((yPoints[i] < scanY && yPoints[j] >= scanY) || (yPoints[j] < scanY && yPoints[i] >= scanY)) {
        int dy = yPoints[j] - yPoints[i];
        if (dy != 0) {
          nodeX[nodes++] = xPoints[i] + (scanY - yPoints[i]) * (xPoints[j] - xPoints[i]) / dy;
        }
      }
      j = i;
    }

    std::sort(nodeX, nodeX + nodes);

    for (int i = 0; i < nodes - 1; i += 2) {
      int startX = nodeX[i];
      int endX = nodeX[i + 1];

      if (startX < 0) startX = 0;
      if (endX >= getScreenWidth()) endX = getScreenWidth() - 1;

      for (int x = startX; x <= endX; x++) {
        drawPixel(x, scanY, state);
      }
    }
  }

  free(nodeX);
}

static unsigned long start_ms = 0;

void GfxRenderer::clearScreen(const uint8_t color) const {
  if (!getWriteTarget()) return;
  start_ms = millis();
  const GrayFrame tuple = activeTuple();
  if (tuple.valid()) {
    memset(tuple.b().data, color, tuple.rows() * tuple.stride());
    memset(tuple.l().data, 0, tuple.rows() * tuple.stride());
    memset(tuple.m().data, 0, tuple.rows() * tuple.stride());
    if (target_.coherent)
      *target_.coherent = !display.postRefreshAborted();
    else
      liveCoherent_ = !display.postRefreshAborted();
    return;
  }
  if (target_.strip) {
    memset(target_.buffer, color, static_cast<size_t>(panelWidthBytes) * target_.rows);
    if (target_.secondary) memset(target_.secondary, color, static_cast<size_t>(panelWidthBytes) * target_.rows);
    return;
  }
  if (readerImportCurrent()) {
    liveCoherent_ = false;
    for (int y = readerImport_.y0; y < readerImport_.y1; ++y) {
      GrayFrame::copyPlaneRow(frameBuffer + y * panelWidthBytes, nullptr, readerImport_.x0, readerImport_.x1, color);
      readerImport_.coverage[y] &= ~1;
    }
    return;
  }
  display.clearScreen(color);
  if (target_.owner == FrameOwner::LiveUi) liveCoherent_ = !display.postRefreshAborted();
}

void GfxRenderer::bindStrip(uint8_t* primary, uint8_t* secondary, int y0, int rows) const {
  target_.buffer = primary;
  target_.secondary = secondary;
  target_.y0 = y0;
  target_.rows = rows;
  target_.strip = true;
  target_.owner = secondary ? FrameOwner::OffscreenSelector : FrameOwner::OffscreenBw;
  if (primary == frameBuffer || secondary == frameBuffer) {
    target_.owner = FrameOwner::ReaderScratch;
    if (uiGrayEnabled_) liveCoherent_ = false;
  }
  target_.coverage = CoveragePolicy::Suspend;
  target_.tuple = {};
  target_.coherent = nullptr;
  target_.identity = ++targetGeneration_;
}

void GfxRenderer::beginStripTarget(uint8_t* scratch, int stripY0, int stripRows) const {
  assert(scratch && stripRows > 0 && stripY0 >= 0 && stripY0 <= static_cast<int>(panelHeight) - stripRows);
  if (!frameBuffer) return;
  if (!stripBound_) {
    stripSaved_ = target_;
    stripSavedMode_ = renderMode;
    stripBound_ = true;
  }
  bindStrip(scratch, nullptr, stripY0, stripRows);
}

void GfxRenderer::beginDualStripTarget(uint8_t* lsb, uint8_t* msb, int stripY0, int stripRows) const {
  assert(msb);
  beginStripTarget(lsb, stripY0, stripRows);
  if (frameBuffer) bindStrip(lsb, msb, stripY0, stripRows);
}

void GfxRenderer::setGrayscaleClipRect(const int x, const int y, const int width, const int height) const {
  target_.x0 = std::max(0, x);
  target_.clipY0 = std::max(0, y);
  target_.x1 = std::min(getScreenWidth(), x + std::max(0, width));
  target_.y1 = std::min(getScreenHeight(), y + std::max(0, height));
  target_.clip = target_.x0 < target_.x1 && target_.clipY0 < target_.y1;
}

void GfxRenderer::endStripTarget() const {
  if (!stripBound_) return;
  accountCancellation();
  target_ = stripSaved_;
  renderMode = stripSavedMode_;
  stripBound_ = false;
  ++targetGeneration_;
}

bool GfxRenderer::glyphIntersectsStrip(int x0, int y0, int x1, int y1) const {
  if (!target_.strip) {
    return true;
  }
  int ax, ay, bx, by;
  rotateCoordinates(orientation, x0, y0, &ax, &ay, panelWidth, panelHeight);
  rotateCoordinates(orientation, x1, y1, &bx, &by, panelWidth, panelHeight);
  const int minY = ay < by ? ay : by;
  const int maxY = ay > by ? ay : by;
  return !(maxY < target_.y0 || minY >= target_.y0 + target_.rows);
}

void GfxRenderer::invertScreen() const {
  uint8_t* target = getWriteTarget();
  if (!target) return;
  const GrayFrame tuple = activeTuple();
  if (tuple.valid()) {
    tuple.invert();
    return;
  }
  const size_t size = static_cast<size_t>(getWriteRows()) * panelWidthBytes;
  for (size_t i = 0; i < size; ++i) target[i] = ~target[i];
}

void GfxRenderer::displayBuffer(const HalDisplay::RefreshMode refreshMode) const {
  if (!canSubmit()) return;
  auto elapsed = millis() - start_ms;
  LOG_DBG("GFX", "Time = %lu ms from clearScreen to displayBuffer", elapsed);
  if (!submitUiGray(refreshMode)) display.displayBuffer(refreshMode, fadingFix);
}

void GfxRenderer::displayBufferAsync(const HalDisplay::RefreshMode refreshMode) const {
  if (!canSubmit()) return;
  if (submitUiGray(refreshMode)) return;
  if (fadingFix) {
    display.displayBuffer(refreshMode, fadingFix);
    return;
  }
  display.displayBufferAsync(refreshMode);
}

bool GfxRenderer::refreshBusy() const { return display.refreshBusy(); }

void GfxRenderer::waitRefreshComplete() const { display.waitRefreshComplete(); }

bool GfxRenderer::hasUiGray() const {
  return uiGrayEnabled_ && target_.owner == FrameOwner::LiveUi &&
         std::any_of(liveM_, liveM_ + frameBufferSize, [](uint8_t value) { return value != 0; });
}

bool GfxRenderer::submitUiGray(HalDisplay::RefreshMode refreshMode) const {
  if (!hasUiGray()) return false;
  display.displayGrayscaleBase(refreshMode, fadingFix);
  if (accountCancellation()) return true;
  display.copyGrayscaleLsbBuffers(liveL_);
  display.copyGrayscaleMsbBuffers(liveM_);
  if (!accountCancellation()) display.displayGrayBuffer(fadingFix);
  return true;
}

bool GfxRenderer::supportsAsyncRefresh() const { return !fadingFix && !hasUiGray() && display.supportsAsyncRefresh(); }

void GfxRenderer::beginDisplayWork() const {
  accountCancellation();
  readerImport_.active = false;
  freeBwBufferChunks();
  ++workGeneration_;
  display.beginDisplayWork();
  cancellationAccounted_ = false;
}

void GfxRenderer::abortDisplayWork() const { display.abortPostRefresh(); }

bool GfxRenderer::displayWorkAborted() const { return display.postRefreshAborted(); }

bool GfxRenderer::displayCommitted() const { return canSubmit() && display.displayCommitted(); }

void GfxRenderer::runDisplayMaintenance() const {
  if (canSubmit()) display.runMaintenance();
}

bool GfxRenderer::hasPendingDisplayMaintenance() const { return display.hasPendingMaintenance(); }

void GfxRenderer::displayControllerIdle() const { display.controllerIdle(); }

size_t GfxRenderer::regionSnapshotBytes(int x, int y, int width, int height) const {
  return snapshotLayout(orientation, x, y, width, height, panelWidth, panelHeight, panelWidthBytes,
                        uiGrayEnabled_ ? 3 : 1)
      .totalBytes;
}

size_t GfxRenderer::frameSnapshotBytes() const {
  return regionSnapshotBytes(0, 0, getScreenWidth(), getScreenHeight());
}

GfxRenderer::FrameResult GfxRenderer::captureRegion(int x, int y, int width, int height, GrayFrame::Plane storage,
                                                    FrameSnapshot& out) const {
  out.valid = false;
  if (!frameBuffer || target_.strip || target_.owner != FrameOwner::LiveUi) return FrameResult::Unavailable;
  if (accountCancellation()) return FrameResult::Cancelled;
  if (!liveCoherent_) return FrameResult::InvalidLive;

  const size_t planeCount = uiGrayEnabled_ ? 3 : 1;
  const auto layout =
      snapshotLayout(orientation, x, y, width, height, panelWidth, panelHeight, panelWidthBytes, planeCount);
  if (!layout.totalBytes || !addressRangeFits(storage.data, layout.totalBytes)) return FrameResult::InvalidSource;
  if (storage.capacity < layout.totalBytes) return FrameResult::ShortCapacity;

  const size_t sourceOffset = static_cast<size_t>(layout.rect.y) * panelWidthBytes + layout.rect.x / 8;
  const size_t sourceBytes = static_cast<size_t>(layout.rect.h - 1) * panelWidthBytes + layout.rowBytes;
  if (sourceOffset > frameBufferSize || sourceBytes > frameBufferSize - sourceOffset) return FrameResult::InvalidLive;
  const uint8_t* sources[] = {frameBuffer, liveL_, liveM_};
  for (size_t plane = 0; plane < 3; ++plane) {
    if (!sources[plane] && plane >= planeCount) continue;
    if (!addressRangeFits(sources[plane], frameBufferSize)) return FrameResult::InvalidLive;
    if (addressRangesOverlap(storage.data, layout.totalBytes, sources[plane], frameBufferSize))
      return FrameResult::InvalidSource;
  }
  for (size_t plane = 0; plane < planeCount; ++plane) {
    for (size_t row = 0; row < layout.rect.h; ++row) {
      memcpy(storage.data + plane * layout.planeBytes + row * layout.rowBytes,
             sources[plane] + sourceOffset + row * panelWidthBytes, layout.rowBytes);
    }
  }
  if (accountCancellation()) return FrameResult::Cancelled;

  const GrayFrame::Plane b{storage.data, layout.planeBytes};
  const GrayFrame::Plane l =
      uiGrayEnabled_ ? GrayFrame::Plane{storage.data + layout.planeBytes, layout.planeBytes} : GrayFrame::Plane{};
  const GrayFrame::Plane m =
      uiGrayEnabled_ ? GrayFrame::Plane{storage.data + 2 * layout.planeBytes, layout.planeBytes} : GrayFrame::Plane{};
  out.planes = GrayFrame(b, l, m, layout.rect.w, layout.rect.h, layout.rowBytes, layout.rect.y);
  out.physicalByteX = layout.rect.x / 8;
  out.panelWidth = panelWidth;
  out.panelHeight = panelHeight;
  out.panelStride = panelWidthBytes;
  out.orientation = orientation;
  out.kind = uiGrayEnabled_ ? SnapshotKind::RetainedTuple : SnapshotKind::LegacyBw;
  out.restoreEpoch = restoreEpoch_;
  out.valid = true;
  return FrameResult::Ok;
}

GfxRenderer::FrameResult GfxRenderer::captureFrame(GrayFrame::Plane storage, FrameSnapshot& out) const {
  return captureRegion(0, 0, getScreenWidth(), getScreenHeight(), storage, out);
}

GfxRenderer::FrameResult GfxRenderer::validateSnapshot(const FrameSnapshot& source) const {
  if (!source.valid || (source.kind != SnapshotKind::RetainedTuple && source.kind != SnapshotKind::LegacyBw))
    return FrameResult::InvalidSource;
  if ((source.kind == SnapshotKind::RetainedTuple) != uiGrayEnabled_) return FrameResult::PolicyMismatch;
  if (source.panelWidth != panelWidth || source.panelHeight != panelHeight || source.panelStride != panelWidthBytes ||
      source.orientation != orientation)
    return FrameResult::GeometryMismatch;
  if (!snapshotGeometryValid(source)) return FrameResult::InvalidSource;
  const size_t destinationOffset = source.planes.y0() * panelWidthBytes + source.physicalByteX;
  const size_t destinationBytes = (source.planes.rows() - 1) * panelWidthBytes + source.planes.stride();
  if (destinationOffset > frameBufferSize || destinationBytes > frameBufferSize - destinationOffset)
    return FrameResult::InvalidLive;
  uint8_t* destinations[] = {frameBuffer, liveL_, liveM_};
  return validateSnapshotPlanes(source, destinations, frameBufferSize, uiGrayEnabled_ ? 3 : 1);
}

GfxRenderer::FrameResult GfxRenderer::restoreRegion(const FrameSnapshot& source) const {
  if (!frameBuffer || target_.strip || target_.owner != FrameOwner::LiveUi) return FrameResult::Unavailable;
  if (accountCancellation()) return FrameResult::Cancelled;
  if (!liveCoherent_ || source.restoreEpoch != restoreEpoch_) return FrameResult::InvalidLive;
  const auto validation = validateSnapshot(source);
  if (validation != FrameResult::Ok) return validation;
  if (accountCancellation()) return FrameResult::Cancelled;

  const uint8_t* sources[] = {source.planes.b().data, source.planes.l().data, source.planes.m().data};
  uint8_t* destinations[] = {frameBuffer, liveL_, liveM_};
  const size_t destinationOffset = source.planes.y0() * panelWidthBytes + source.physicalByteX;
  const size_t planeCount = uiGrayEnabled_ ? 3 : 1;
  for (size_t plane = 0; plane < planeCount; ++plane) {
    for (size_t row = 0; row < source.planes.rows(); ++row) {
      memcpy(destinations[plane] + destinationOffset + row * panelWidthBytes,
             sources[plane] + row * source.planes.stride(), source.planes.stride());
    }
  }
  return accountCancellation() ? FrameResult::PublishedCancelled : FrameResult::Ok;
}

GfxRenderer::FrameResult GfxRenderer::replaceFrame(const FrameSnapshot& source) const {
  if (!frameBuffer || target_.strip || target_.owner != FrameOwner::LiveUi) return FrameResult::Unavailable;
  if (display.postRefreshAborted()) return FrameResult::Cancelled;
  const auto validation = validateSnapshot(source);
  if (validation != FrameResult::Ok) return validation;
  if (source.planes.width() != panelWidth || source.planes.rows() != panelHeight) return FrameResult::GeometryMismatch;
  if (display.postRefreshAborted()) return FrameResult::Cancelled;

  const uint8_t* sources[] = {source.planes.b().data, source.planes.l().data, source.planes.m().data};
  uint8_t* destinations[] = {frameBuffer, liveL_, liveM_};
  const size_t bytes = source.planes.stride() * source.planes.rows();
  for (size_t plane = 0; plane < (uiGrayEnabled_ ? 3u : 1u); ++plane) {
    memcpy(destinations[plane], sources[plane], bytes);
  }
  ++targetGeneration_;
  freeBwBufferChunks();
  readerImport_ = {};
  if (accountCancellation()) return FrameResult::PublishedCancelled;
  ++restoreEpoch_;
  liveCoherent_ = true;
  return FrameResult::Ok;
}

size_t GfxRenderer::readFramebufferRegion(int x, int y, int w, int h, uint8_t* dst, size_t dstCapacity) const {
  if (uiGrayEnabled_ || !canCaptureLiveFrame() || dst == nullptr || w <= 0 || h <= 0) return 0;

  const AlignedMemRect mem = screenRectToAlignedMemRect(orientation, x, y, w, h, panelWidth, panelHeight);
  if (!mem.valid) return 0;

  const size_t rowBytes = mem.w / 8;  // exact: mem.w is a multiple of 8
  const size_t needed = rowBytes * mem.h;
  if (needed > dstCapacity) return 0;

  for (uint16_t row = 0; row < mem.h; ++row) {
    const uint8_t* srcRow = frameBuffer + (static_cast<uint32_t>(mem.y + row) * panelWidthBytes) + (mem.x / 8);
    uint8_t* dstRow = dst + (static_cast<size_t>(row) * rowBytes);
    memcpy(dstRow, srcRow, rowBytes);
  }
  return needed;
}

void GfxRenderer::writeFramebufferRegion(int x, int y, int w, int h, const uint8_t* src) {
  if (uiGrayEnabled_ || !canCaptureLiveFrame() || src == nullptr || w <= 0 || h <= 0) return;

  const AlignedMemRect mem = screenRectToAlignedMemRect(orientation, x, y, w, h, panelWidth, panelHeight);
  if (!mem.valid) return;

  const size_t rowBytes = mem.w / 8;  // exact: mem.w is a multiple of 8

  for (uint16_t row = 0; row < mem.h; ++row) {
    const uint8_t* srcRow = src + (static_cast<size_t>(row) * rowBytes);
    uint8_t* dstRow = frameBuffer + (static_cast<uint32_t>(mem.y + row) * panelWidthBytes) + (mem.x / 8);
    memcpy(dstRow, srcRow, rowBytes);
  }
}

std::string GfxRenderer::truncatedText(const int fontId, const char* text, const int maxWidth,
                                       const EpdFontFamily::Style style) const {
  if (!text || maxWidth <= 0) return "";

  std::string item = text;
  const char* ellipsis = "\xe2\x80\xa6";
  int textWidth = getTextWidth(fontId, item.c_str(), style);
  if (textWidth <= maxWidth) {
    return item;
  }

  while (!item.empty() && getTextWidth(fontId, (item + ellipsis).c_str(), style) >= maxWidth) {
    utf8RemoveLastChar(item);
  }

  return item.empty() ? ellipsis : item + ellipsis;
}

std::vector<std::string> GfxRenderer::wrappedText(const int fontId, const char* text, const int maxWidth,
                                                  const int maxLines, const EpdFontFamily::Style style) const {
  std::vector<std::string> lines;

  if (!text || maxWidth <= 0 || maxLines <= 0) return lines;

  std::string remaining = text;
  std::string currentLine;

  while (!remaining.empty()) {
    if (static_cast<int>(lines.size()) == maxLines - 1) {
      std::string lastContent = currentLine.empty() ? remaining : currentLine + " " + remaining;
      lines.push_back(truncatedText(fontId, lastContent.c_str(), maxWidth, style));
      return lines;
    }

    size_t spacePos = remaining.find(' ');
    std::string word;

    if (spacePos == std::string::npos) {
      word = remaining;
      remaining.clear();
    } else {
      word = remaining.substr(0, spacePos);
      remaining.erase(0, spacePos + 1);
    }

    std::string testLine = currentLine.empty() ? word : currentLine + " " + word;

    if (getTextWidth(fontId, testLine.c_str(), style) <= maxWidth) {
      currentLine = testLine;
    } else {
      if (!currentLine.empty()) {
        lines.push_back(currentLine);
        if (getTextWidth(fontId, word.c_str(), style) > maxWidth) {
          lines.push_back(truncatedText(fontId, word.c_str(), maxWidth, style));
          currentLine.clear();
          if (static_cast<int>(lines.size()) >= maxLines) return lines;
        } else {
          currentLine = word;
        }
      } else {
        lines.push_back(truncatedText(fontId, word.c_str(), maxWidth, style));
        return lines;
      }
    }
  }

  if (!currentLine.empty() && static_cast<int>(lines.size()) < maxLines) {
    lines.push_back(currentLine);
  }

  return lines;
}

int GfxRenderer::getScreenWidth() const {
  switch (orientation) {
    case Portrait:
    case PortraitInverted:
      return panelHeight;
    case LandscapeClockwise:
    case LandscapeCounterClockwise:
      return panelWidth;
  }
  return panelHeight;
}

int GfxRenderer::getScreenHeight() const {
  switch (orientation) {
    case Portrait:
    case PortraitInverted:
      return panelWidth;
    case LandscapeClockwise:
    case LandscapeCounterClockwise:
      return panelHeight;
  }
  return panelWidth;
}

void GfxRenderer::tapToLogical(float nx, float ny, int& outX, int& outY) const {
  int phyX = static_cast<int>(nx * panelWidth);
  int phyY = static_cast<int>(ny * panelHeight);
  if (phyX < 0) phyX = 0;
  if (phyX > panelWidth - 1) phyX = panelWidth - 1;
  if (phyY < 0) phyY = 0;
  if (phyY > panelHeight - 1) phyY = panelHeight - 1;

  switch (orientation) {
    case Portrait:
      outX = panelHeight - 1 - phyY;
      outY = phyX;
      break;
    case PortraitInverted:
      outX = phyY;
      outY = panelWidth - 1 - phyX;
      break;
    case LandscapeClockwise:
      outX = panelWidth - 1 - phyX;
      outY = panelHeight - 1 - phyY;
      break;
    case LandscapeCounterClockwise:
    default:
      outX = phyX;
      outY = phyY;
      break;
  }
}

static bool logicalRectToPhysicalBounds(GfxRenderer::Orientation orientation, int lx, int ly, int lw, int lh,
                                        uint16_t panelWidth, uint16_t panelHeight, int* outX0, int* outY0, int* outX1,
                                        int* outY1) {
  if (lw <= 0 || lh <= 0) return false;
  int minX = INT32_MAX;
  int minY = INT32_MAX;
  int maxX = INT32_MIN;
  int maxY = INT32_MIN;
  const int corners[4][2] = {{lx, ly}, {lx + lw - 1, ly}, {lx, ly + lh - 1}, {lx + lw - 1, ly + lh - 1}};
  for (auto& c : corners) {
    int phyX;
    int phyY;
    rotateCoordinates(orientation, c[0], c[1], &phyX, &phyY, panelWidth, panelHeight);
    if (phyX < minX) minX = phyX;
    if (phyY < minY) minY = phyY;
    if (phyX > maxX) maxX = phyX;
    if (phyY > maxY) maxY = phyY;
  }
  if (minX < 0) minX = 0;
  if (minY < 0) minY = 0;
  if (maxX >= panelWidth) maxX = panelWidth - 1;
  if (maxY >= panelHeight) maxY = panelHeight - 1;
  if (minX > maxX || minY > maxY) return false;
  *outX0 = minX;
  *outY0 = minY;
  *outX1 = maxX;
  *outY1 = maxY;
  return true;
}

size_t GfxRenderer::getRegionByteSize(int lx, int ly, int lw, int lh) const {
  int x0, y0, x1, y1;
  if (!logicalRectToPhysicalBounds(orientation, lx, ly, lw, lh, panelWidth, panelHeight, &x0, &y0, &x1, &y1)) {
    return 0;
  }
  const int byteX0 = x0 / 8;
  const int byteX1 = x1 / 8;
  const int bytesPerRow = byteX1 - byteX0 + 1;
  const int rowCount = y1 - y0 + 1;
  return static_cast<size_t>(bytesPerRow) * static_cast<size_t>(rowCount);
}

bool GfxRenderer::copyRegionToBuffer(int lx, int ly, int lw, int lh, uint8_t* buf, size_t bufSize) const {
  if (uiGrayEnabled_) return false;
  int x0, y0, x1, y1;
  if (!logicalRectToPhysicalBounds(orientation, lx, ly, lw, lh, panelWidth, panelHeight, &x0, &y0, &x1, &y1)) {
    return false;
  }
  const int byteX0 = x0 / 8;
  const int byteX1 = x1 / 8;
  const int bytesPerRow = byteX1 - byteX0 + 1;
  const int rowCount = y1 - y0 + 1;
  const size_t needed = static_cast<size_t>(bytesPerRow) * static_cast<size_t>(rowCount);
  if (bufSize < needed || !canCaptureLiveFrame() || !buf) return false;
  for (int row = 0; row < rowCount; row++) {
    const uint8_t* src = frameBuffer + (y0 + row) * panelWidthBytes + byteX0;
    memcpy(buf + row * bytesPerRow, src, bytesPerRow);
  }
  return true;
}

bool GfxRenderer::copyBufferToRegion(int lx, int ly, int lw, int lh, const uint8_t* buf, size_t bufSize) const {
  if (uiGrayEnabled_) return false;
  int x0, y0, x1, y1;
  if (!logicalRectToPhysicalBounds(orientation, lx, ly, lw, lh, panelWidth, panelHeight, &x0, &y0, &x1, &y1)) {
    return false;
  }
  const int byteX0 = x0 / 8;
  const int byteX1 = x1 / 8;
  const int bytesPerRow = byteX1 - byteX0 + 1;
  const int rowCount = y1 - y0 + 1;
  const size_t needed = static_cast<size_t>(bytesPerRow) * static_cast<size_t>(rowCount);
  if (bufSize < needed || !canCaptureLiveFrame() || !buf) return false;
  for (int row = 0; row < rowCount; row++) {
    uint8_t* dst = frameBuffer + (y0 + row) * panelWidthBytes + byteX0;
    memcpy(dst, buf + row * bytesPerRow, bytesPerRow);
  }
  return true;
}

int GfxRenderer::getSpaceWidth(const int fontId, const EpdFontFamily::Style style) const {
  auto sdIt = sdCardFonts_.find(fontId);
  if (sdIt != sdCardFonts_.end() && sdIt->second->hasAdvanceTable()) {
    const uint8_t resolvedStyle = resolveSdCardStyle(*sdIt->second, style);
    return fp4::toPixel(sdIt->second->getAdvance(' ', resolvedStyle));
  }

  const auto fontIt = fontMap.find(fontId);
  if (fontIt == fontMap.end()) {
    LOG_ERR("GFX", "Font %d not found", fontId);
    return 0;
  }

  const EpdFontData* data = fontIt->second.getData(style);
  if (data->advanceHandler) {
    return fp4::toPixel(data->advanceHandler(data->glyphMissCtx, ' '));
  }
  const EpdGlyph* spaceGlyph = fontIt->second.getGlyph(' ', style);
  return spaceGlyph ? fp4::toPixel(spaceGlyph->advanceX) : 0;  // snap 12.4 fixed-point to nearest pixel
}

int GfxRenderer::getSpaceAdvance(const int fontId, const uint32_t leftCp, const uint32_t rightCp,
                                 const EpdFontFamily::Style style) const {
  auto sdIt = sdCardFonts_.find(fontId);
  if (sdIt != sdCardFonts_.end() && sdIt->second->hasAdvanceTable()) {
    const uint8_t resolvedStyle = resolveSdCardStyle(*sdIt->second, style);
    return fp4::toPixel(sdIt->second->getAdvance(' ', resolvedStyle));
  }

  const auto fontIt = fontMap.find(fontId);
  if (fontIt == fontMap.end()) return 0;
  const auto& font = fontIt->second;
  const EpdFontData* data = font.getData(style);
  const EpdGlyph* spaceGlyph = data->advanceHandler ? nullptr : font.getGlyph(' ', style);
  const int32_t spaceAdvanceFP = data->advanceHandler
                                     ? static_cast<int32_t>(data->advanceHandler(data->glyphMissCtx, ' '))
                                     : (spaceGlyph ? static_cast<int32_t>(spaceGlyph->advanceX) : 0);
  const int32_t kernFP = static_cast<int32_t>(font.getKerning(leftCp, ' ', style)) +
                         static_cast<int32_t>(font.getKerning(' ', rightCp, style));
  return fp4::toPixel(spaceAdvanceFP + kernFP);
}

int GfxRenderer::getKerning(const int fontId, const uint32_t leftCp, const uint32_t rightCp,
                            const EpdFontFamily::Style style) const {
  const auto fontIt = fontMap.find(fontId);
  if (fontIt == fontMap.end()) return 0;
  const int kernFP = fontIt->second.getKerning(leftCp, rightCp, style);  // 4.4 fixed-point
  return fp4::toPixel(kernFP);                                           // snap 4.4 fixed-point to nearest pixel
}

int GfxRenderer::getTextAdvanceX(const int fontId, const char* text, EpdFontFamily::Style style) const {
  const int resolvedFontId = resolveTextFontId(fontId, text, style);
  std::string visual;
  text = resolveVisualText(text, visual, BidiUtils::BidiBaseDir::AUTO);

  auto sdIt = sdCardFonts_.find(resolvedFontId);
  if (sdIt != sdCardFonts_.end() && sdIt->second->hasAdvanceTable()) {
    int32_t widthFP = 0;
    const bool isSupSub = (style & (EpdFontFamily::SUP | EpdFontFamily::SUB)) != 0;
    const uint8_t styleIdx = resolveSdCardStyle(*sdIt->second, style);
    const auto fontIt = fontMap.find(resolvedFontId);
    if (fontIt == fontMap.end()) {
      LOG_ERR("GFX", "Font %d not found", resolvedFontId);
      return 0;
    }
    const auto& font = fontIt->second;
    while (uint32_t cp = utf8NextCodepoint(reinterpret_cast<const uint8_t**>(&text))) {
      if (BidiUtils::isTransparentMark(cp)) {
        continue;
      }
      int32_t advFP = sdIt->second->getAdvance(cp, styleIdx);
      if (advFP == 0 && !utf8IsCombiningMark(cp)) {
        const EpdGlyph* glyph = font.getGlyph(cp, style);
        advFP = glyph ? glyph->advanceX : 0;
      }
      widthFP += isSupSub ? (advFP + 1) / 2 : advFP;
    }
    return fp4::toPixel(widthFP);
  }

  const auto fontIt = fontMap.find(resolvedFontId);
  if (fontIt == fontMap.end()) {
    LOG_ERR("GFX", "Font %d not found", resolvedFontId);
    return 0;
  }

  uint32_t cp;
  uint32_t prevCp = 0;
  int widthPx = 0;
  int32_t prevAdvanceFP = 0;  // 12.4 fixed-point: prev glyph's advance + next kern for snap
  const auto& font = fontIt->second;
  const EpdFontData* data = font.getData(style);
  while ((cp = utf8NextCodepoint(reinterpret_cast<const uint8_t**>(&text)))) {
    if (BidiUtils::isTransparentMark(cp)) {
      continue;
    }
    if (utf8IsCombiningMark(cp)) {
      continue;
    }
    cp = font.applyLigatures(cp, text, style);

    if (prevCp != 0) {
      const auto kernFP = font.getKerning(prevCp, cp, style);  // 4.4 fixed-point kern
      widthPx += fp4::toPixel(prevAdvanceFP + kernFP);         // snap 12.4 fixed-point to nearest pixel
    }

    if (data->advanceHandler) {
      prevAdvanceFP = data->advanceHandler(data->glyphMissCtx, cp);
    } else {
      const EpdGlyph* glyph = font.getGlyph(cp, style);
      prevAdvanceFP = glyph ? glyph->advanceX : 0;
    }
    if ((style & (EpdFontFamily::SUP | EpdFontFamily::SUB)) != 0) {
      prevAdvanceFP = (prevAdvanceFP + 1) / 2;
    }
    prevCp = cp;
  }
  widthPx += fp4::toPixel(prevAdvanceFP);  // final glyph's advance
  return widthPx;
}

int GfxRenderer::getFontAscenderSize(const int fontId) const {
  const auto fontIt = fontMap.find(fontId);
  if (fontIt == fontMap.end()) {
    LOG_ERR("GFX", "Font %d not found", fontId);
    return 0;
  }

  return fontIt->second.getData(EpdFontFamily::REGULAR)->ascender;
}

int GfxRenderer::getLineHeight(const int fontId) const {
  const auto fontIt = fontMap.find(fontId);
  if (fontIt == fontMap.end()) {
    LOG_ERR("GFX", "Font %d not found", fontId);
    return 0;
  }

  return fontIt->second.getData(EpdFontFamily::REGULAR)->advanceY;
}

int GfxRenderer::getLineHeight(const int fontId, const float compression) const {
  return static_cast<int>(getLineHeight(fontId) * compression + 0.5f);
}

int GfxRenderer::getLineHeightForText(const int fontId, const char* text, const EpdFontFamily::Style style) const {
  return getLineHeight(resolveTextFontId(fontId, text, style));
}

int GfxRenderer::getTextHeight(const int fontId) const {
  const auto fontIt = fontMap.find(fontId);
  if (fontIt == fontMap.end()) {
    LOG_ERR("GFX", "Font %d not found", fontId);
    return 0;
  }
  return fontIt->second.getData(EpdFontFamily::REGULAR)->ascender;
}

void GfxRenderer::drawTextRotated90CW(const int fontId, const int x, const int y, const char* text, const bool black,
                                      const EpdFontFamily::Style style) const {
  if (text == nullptr || *text == '\0') {
    return;
  }

  const int resolvedFontId = resolveTextFontId(fontId, text, style);
  const auto fontIt = fontMap.find(resolvedFontId);
  if (fontIt == fontMap.end()) {
    LOG_ERR("GFX", "Font %d not found", resolvedFontId);
    return;
  }

  const auto& font = fontIt->second;

  int lastBaseY = y;
  int lastBaseLeft = 0;
  int lastBaseWidth = 0;
  int lastBaseTop = 0;
  int32_t prevAdvanceFP = 0;  // 12.4 fixed-point: prev glyph's advance + next kern for snap

  uint32_t cp;
  uint32_t prevCp = 0;
  while ((cp = utf8NextCodepoint(reinterpret_cast<const uint8_t**>(&text)))) {
    if (utf8IsCombiningMark(cp) || BidiUtils::isTransparentMark(cp)) {
      const EpdGlyph* combiningGlyph = font.getGlyph(cp, style);
      if (!combiningGlyph) continue;
      const auto anchor = combiningMark::anchorFor(cp);
      const int raiseBy =
          combiningMark::raiseAboveBase(anchor, combiningGlyph->top, combiningGlyph->height, lastBaseTop);
      const int combiningX = x - raiseBy;
      const int combiningY = combiningMark::anchorOverRotated90CW(anchor, lastBaseY, lastBaseLeft, lastBaseWidth,
                                                                  combiningGlyph->left, combiningGlyph->width);
      renderCharImpl<TextRotation::Rotated90CW>(*this, renderMode, font, cp, combiningX, combiningY, black, style);
      continue;
    }

    cp = font.applyLigatures(cp, text, style);

    if (prevCp != 0) {
      const auto kernFP = font.getKerning(prevCp, cp, style);  // 4.4 fixed-point kern
      lastBaseY -= fp4::toPixel(prevAdvanceFP + kernFP);       // snap 12.4 fixed-point to nearest pixel
    }

    const EpdGlyph* glyph = font.getGlyph(cp, style);

    lastBaseLeft = glyph ? glyph->left : 0;
    lastBaseWidth = glyph ? glyph->width : 0;
    lastBaseTop = glyph ? glyph->top : 0;
    prevAdvanceFP = glyph ? glyph->advanceX : 0;  // 12.4 fixed-point

    renderCharImpl<TextRotation::Rotated90CW>(*this, renderMode, font, cp, x, lastBaseY, black, style);
    prevCp = cp;
  }
}

uint8_t* GfxRenderer::getFrameBuffer() const { return frameBuffer; }

size_t GfxRenderer::getBufferSize() const { return frameBufferSize; }

void GfxRenderer::displayGrayscaleBase(HalDisplay::RefreshMode fallback) const {
  if (!canSubmit()) return;
  display.displayGrayscaleBase(fallback, fadingFix);
}

void GfxRenderer::preconditionGrayscale() const {
  if (canSubmit()) display.preconditionGrayscale();
}

void GfxRenderer::preconditionGrayscale(int x, int y, int w, int h) const {
  if (!canSubmit()) return;
  if (w <= 0 || h <= 0) return;
  int ax, ay, bx, by;
  rotateCoordinates(orientation, x, y, &ax, &ay, panelWidth, panelHeight);
  rotateCoordinates(orientation, x + w - 1, y + h - 1, &bx, &by, panelWidth, panelHeight);
  int x0 = ax < bx ? ax : bx, x1 = ax > bx ? ax : bx;
  int y0 = ay < by ? ay : by, y1 = ay > by ? ay : by;
  if (x0 < 0) x0 = 0;
  if (y0 < 0) y0 = 0;
  if (x1 >= panelWidth) x1 = panelWidth - 1;
  if (y1 >= panelHeight) y1 = panelHeight - 1;
  if (x1 < x0 || y1 < y0) return;
  display.preconditionGrayscale(static_cast<uint16_t>(x0), static_cast<uint16_t>(y0),
                                static_cast<uint16_t>(x1 - x0 + 1), static_cast<uint16_t>(y1 - y0 + 1));
}

bool GfxRenderer::readerImportCurrent() const {
  return readerImport_.active && frameBuffer && target_.owner == FrameOwner::ReaderScratch && !target_.strip &&
         readerImport_.target == target_.identity && readerImport_.work == workGeneration_ &&
         readerImport_.orientation == orientation && !accountCancellation();
}

bool GfxRenderer::beginReaderImport(int x, int y, int width, int height) const {
  if (!uiGrayEnabled_ || !frameBuffer || target_.strip || target_.owner != FrameOwner::ReaderScratch ||
      !target_.coherentOnEntry || accountCancellation())
    return false;
  setGrayscaleClipRect(x, y, width, height);
  if (!target_.clip) return false;
  int x0, y0, x1, y1;
  if (!logicalRectToPhysicalBounds(orientation, target_.x0, target_.clipY0, target_.x1 - target_.x0,
                                   target_.y1 - target_.clipY0, panelWidth, panelHeight, &x0, &y0, &x1, &y1))
    return false;
  readerImport_ = {};
  readerImport_.active = true;
  readerImport_.target = target_.identity;
  readerImport_.work = workGeneration_;
  readerImport_.orientation = orientation;
  readerImport_.x0 = x0;
  readerImport_.x1 = x1 + 1;
  readerImport_.y0 = y0;
  readerImport_.y1 = y1 + 1;
  target_.coherentOnEntry = false;
  return true;
}

bool GfxRenderer::replaceReaderRows(uint8_t* destination, uint8_t coverage, const uint8_t* source, size_t capacity,
                                    int yStart, int rows) const {
  if (!readerImportCurrent() || !source || yStart < 0 || rows <= 0 || yStart > panelHeight - rows ||
      static_cast<size_t>(rows) > capacity / panelWidthBytes)
    return false;
  const int first = std::max(yStart, readerImport_.y0);
  const int last = std::min(yStart + rows, readerImport_.y1);
  if (first >= last) return false;
  liveCoherent_ = false;
  for (int y = first; y < last; ++y) {
    GrayFrame::copyPlaneRow(destination + y * panelWidthBytes, source + (y - yStart) * panelWidthBytes,
                            readerImport_.x0, readerImport_.x1);
    readerImport_.coverage[y] |= coverage;
  }
  return true;
}

bool GfxRenderer::importReaderPlane(bool lsb, const uint8_t* source, size_t capacity, int yStart, int rows) const {
  uint8_t* destination = lsb ? liveL_ : liveM_;
  if (!replaceReaderRows(destination, lsb ? 2 : 4, source, capacity, yStart, rows)) return false;
  const int first = std::max(yStart, readerImport_.y0);
  const int last = std::min(yStart + rows, readerImport_.y1);
  display.writeGrayscalePlaneStrip(lsb, destination + first * panelWidthBytes, first, last - first);
  return true;
}

bool GfxRenderer::restoreReaderBase(const uint8_t* source, size_t capacity, int yStart, int rows) const {
  return replaceReaderRows(frameBuffer, 1, source, capacity, yStart, rows);
}

bool GfxRenderer::finishReaderImport() const {
  if (!readerImportCurrent()) return false;
  for (int y = readerImport_.y0; y < readerImport_.y1; ++y) {
    if (readerImport_.coverage[y] != 7) return false;
  }
  liveCoherent_ = true;
  return true;
}

void GfxRenderer::copyGrayscaleLsbBuffers() const {
  if (uiGrayEnabled_) {
    importReaderPlane(true, frameBuffer, frameBufferSize, 0, panelHeight);
    return;
  }
  if (frameBuffer && !target_.strip) display.copyGrayscaleLsbBuffers(frameBuffer);
}

void GfxRenderer::copyGrayscaleMsbBuffers() const {
  if (uiGrayEnabled_) {
    importReaderPlane(false, frameBuffer, frameBufferSize, 0, panelHeight);
    return;
  }
  if (frameBuffer && !target_.strip) display.copyGrayscaleMsbBuffers(frameBuffer);
}

void GfxRenderer::displayGrayBuffer() const {
  if (!canSubmit()) return;
  if (uiGrayEnabled_) {
    display.copyGrayscaleLsbBuffers(liveL_);
    display.copyGrayscaleMsbBuffers(liveM_);
  }
  if (!accountCancellation()) display.displayGrayBuffer(fadingFix);
}

void GfxRenderer::displayGrayCalibration(const int customX, const int customY, const int customW,
                                         const int customH) const {
  const AlignedMemRect rect =
      screenRectToAlignedMemRect(orientation, customX, customY, customW, customH, panelWidth, panelHeight);
  if (!rect.valid) return;
  if (canSubmit()) display.displayGrayCalibration(rect.x, rect.y, rect.w, rect.h);
}

void GfxRenderer::writeGrayscalePlaneStrip(bool lsbPlane, const uint8_t* scratch, int yStart, int numRows) const {
  if (!frameBuffer) return;
  if (uiGrayEnabled_) {
    importReaderPlane(lsbPlane, scratch, numRows > 0 ? static_cast<size_t>(numRows) * panelWidthBytes : 0, yStart,
                      numRows);
    return;
  }
  assert(yStart >= 0 && numRows > 0 && yStart <= static_cast<int>(panelHeight) - numRows);
  display.writeGrayscalePlaneStrip(lsbPlane, scratch, static_cast<uint16_t>(yStart), static_cast<uint16_t>(numRows));
}

bool GfxRenderer::supportsBusyGrayscaleStaging() const { return display.supportsBusyGrayscaleStaging(); }

void GfxRenderer::prepareGrayscaleTarget() const {
  if (frameBuffer) display.prepareGrayscaleTarget();
}

bool GfxRenderer::supportsStripGrayscale() const { return display.supportsStripGrayscale(); }

void GfxRenderer::freeBwBufferChunks() const {
  for (auto& chunk : bwBufferChunks) {
    free(chunk);
    chunk = nullptr;
  }
  bwBufferStored_ = false;
  bwScratchOwner_ = 0;
  bwScratchWork_ = 0;
}

bool GfxRenderer::storeBwBufferChunks() const {
  if (bwBufferStored_ || bwBufferChunks.empty()) return false;
  for (size_t i = 0; i < bwBufferChunks.size(); ++i) {
    const size_t offset = i * BW_BUFFER_CHUNK_SIZE;
    const size_t chunkSize = std::min(BW_BUFFER_CHUNK_SIZE, static_cast<size_t>(frameBufferSize - offset));
    bwBufferChunks[i] = static_cast<uint8_t*>(malloc(chunkSize));
    if (!bwBufferChunks[i]) {
      LOG_ERR("GFX", "Failed to allocate BW buffer chunk %zu (%zu bytes)", i, chunkSize);
      freeBwBufferChunks();
      return false;
    }
    memcpy(bwBufferChunks[i], frameBuffer + offset, chunkSize);
  }
  bwBufferStored_ = true;
  return true;
}

bool GfxRenderer::restoreBwBufferChunks() const {
  if (!bwBufferStored_) return false;
  if (bwBufferChunks.size() != (frameBufferSize + BW_BUFFER_CHUNK_SIZE - 1) / BW_BUFFER_CHUNK_SIZE ||
      std::any_of(bwBufferChunks.begin(), bwBufferChunks.end(), [](const uint8_t* chunk) { return !chunk; })) {
    freeBwBufferChunks();
    return false;
  }
  for (size_t i = 0; i < bwBufferChunks.size(); ++i) {
    const size_t offset = i * BW_BUFFER_CHUNK_SIZE;
    const size_t chunkSize = std::min(BW_BUFFER_CHUNK_SIZE, static_cast<size_t>(frameBufferSize - offset));
    memcpy(frameBuffer + offset, bwBufferChunks[i], chunkSize);
  }
  freeBwBufferChunks();
  return true;
}

bool GfxRenderer::storeReaderBwScratch() {
  if (!frameBuffer || target_.strip || target_.owner != FrameOwner::ReaderScratch || accountCancellation())
    return false;
  if (!storeBwBufferChunks()) return false;
  bwScratchOwner_ = target_.identity;
  bwScratchWork_ = workGeneration_;
  return true;
}

bool GfxRenderer::restoreReaderBwScratch() {
  if (!frameBuffer || target_.strip || target_.owner != FrameOwner::ReaderScratch ||
      bwScratchOwner_ != target_.identity || bwScratchWork_ != workGeneration_)
    return false;
  accountCancellation();
  if (!restoreBwBufferChunks()) return false;
  if (uiGrayEnabled_) liveCoherent_ = false;
  if (readerImportCurrent()) {
    for (int y = readerImport_.y0; y < readerImport_.y1; ++y) readerImport_.coverage[y] |= 1;
  }
  return true;
}

bool GfxRenderer::storeBwBuffer() { return !uiGrayEnabled_ && canCaptureLiveFrame() && storeBwBufferChunks(); }

void GfxRenderer::restoreBwBuffer() {
  if (uiGrayEnabled_ || !frameBuffer || target_.strip || bwScratchOwner_ != 0) return;
  if (restoreBwBufferChunks()) cleanupGrayscaleWithFrameBuffer();
}

void GfxRenderer::cleanupGrayscaleWithFrameBuffer() const {
  if (frameBuffer) {
    display.cleanupGrayscaleBuffers(frameBuffer);
  }
}

void GfxRenderer::getOrientedViewableTRBL(int* outTop, int* outRight, int* outBottom, int* outLeft) const {
  switch (orientation) {
    case Portrait:
      *outTop = VIEWABLE_MARGIN_TOP;
      *outRight = VIEWABLE_MARGIN_RIGHT;
      *outBottom = VIEWABLE_MARGIN_BOTTOM;
      *outLeft = VIEWABLE_MARGIN_LEFT;
      break;
    case LandscapeClockwise:
      *outTop = VIEWABLE_MARGIN_LEFT;
      *outRight = VIEWABLE_MARGIN_TOP;
      *outBottom = VIEWABLE_MARGIN_RIGHT;
      *outLeft = VIEWABLE_MARGIN_BOTTOM;
      break;
    case PortraitInverted:
      *outTop = VIEWABLE_MARGIN_BOTTOM;
      *outRight = VIEWABLE_MARGIN_LEFT;
      *outBottom = VIEWABLE_MARGIN_TOP;
      *outLeft = VIEWABLE_MARGIN_RIGHT;
      break;
    case LandscapeCounterClockwise:
      *outTop = VIEWABLE_MARGIN_RIGHT;
      *outRight = VIEWABLE_MARGIN_BOTTOM;
      *outBottom = VIEWABLE_MARGIN_LEFT;
      *outLeft = VIEWABLE_MARGIN_TOP;
      break;
  }
}
