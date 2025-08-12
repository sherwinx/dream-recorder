/**
 * Demo Background Manager
 * Accelerated version that cycles through all backgrounds in 4 hours instead of 24 hours
 */

class DemoBackgroundManager {
    constructor() {
        this.container = document.getElementById('container');
        this.currentImage = null;
        this.fadeDuration = 2000; // Increased to 2 seconds for smoother transitions
        this.updateInterval = 10000; // Update every 10 seconds for more frequent changes
        this.isLoading = false;
        this.totalImages = parseInt(document.body.dataset.totalBackgroundImages);
        
        // Cycle duration: 4 hours instead of 24 hours
        this.cycleDurationHours = 4;
        
        // Preload the first image
        this.preloadNextImage().then(() => {
            this.start();
        });
    }

    async preloadNextImage() {
        const newImagePath = this.getImagePath();
        if (newImagePath === this.currentImage) return;

        return new Promise((resolve) => {
            const img = new Image();
            img.onload = () => {
                this.nextImage = img;
                resolve();
            };
            img.onerror = () => {
                console.error('Failed to load image:', newImagePath);
                resolve();
            };
            img.src = newImagePath;
        });
    }

    getImageNumberForTime() {
        const now = new Date();
        // Get total minutes since midnight
        const minutesInDay = now.getHours() * 60 + now.getMinutes();
        
        // Map to our accelerated cycle (4 hours = 240 minutes)
        const cycleDurationMinutes = this.cycleDurationHours * 60;
        
        // Get position within current cycle
        const minutesInCycle = minutesInDay % cycleDurationMinutes;
        
        // Calculate which image to show based on position in cycle
        const imageNumber = Math.floor((minutesInCycle / cycleDurationMinutes) * this.totalImages);
        
        console.log(`Time: ${now.toLocaleTimeString()}, Minutes in cycle: ${minutesInCycle}, Image: ${imageNumber}`);
        
        return Math.min(Math.max(imageNumber, 0), this.totalImages - 1);
    }

    getImagePath() {
        const imageNumber = this.getImageNumberForTime();
        return `/static/images/background/${imageNumber}.jpg`;
    }

    async fadeToNewBackground(newImagePath) {
        // Create a temporary div for the new background
        const tempDiv = document.createElement('div');
        tempDiv.style.position = 'absolute';
        tempDiv.style.top = '0';
        tempDiv.style.left = '0';
        tempDiv.style.width = '100%';
        tempDiv.style.height = '100%';
        tempDiv.style.backgroundImage = `url('${newImagePath}')`;
        tempDiv.style.backgroundSize = 'cover';
        tempDiv.style.backgroundPosition = 'center';
        tempDiv.style.backgroundRepeat = 'no-repeat';
        tempDiv.style.opacity = '0';
        tempDiv.style.transition = `opacity ${this.fadeDuration}ms ease-in-out`;
        tempDiv.style.zIndex = '0';
        this.container.appendChild(tempDiv);

        // Fade in the new background
        setTimeout(() => {
            tempDiv.style.opacity = '1';
        }, 10);

        // After fade completes, update the main background and remove the temp div
        return new Promise(resolve => {
            setTimeout(() => {
                this.container.style.backgroundImage = `url('${newImagePath}')`;
                if (tempDiv.parentNode) {
                    this.container.removeChild(tempDiv);
                }
                this.currentImage = newImagePath;
                resolve();
            }, this.fadeDuration);
        });
    }

    async changeBackground() {
        if (this.isLoading) return;
        
        const newImagePath = this.getImagePath();
        if (newImagePath === this.currentImage) return;

        this.isLoading = true;

        try {
            // If we have a preloaded image, use it
            if (this.nextImage && this.nextImage.src.includes(newImagePath)) {
                await this.fadeToNewBackground(newImagePath);
            } else {
                // Otherwise, load the new image
                const img = new Image();
                await new Promise((resolve, reject) => {
                    img.onload = resolve;
                    img.onerror = reject;
                    img.src = newImagePath;
                });
                await this.fadeToNewBackground(newImagePath);
            }

            // Preload the next image
            await this.preloadNextImage();
        } catch (error) {
            console.error('Error changing background:', error);
        } finally {
            this.isLoading = false;
        }
    }

    start() {
        // Initial background set
        this.changeBackground();
        
        // Set up interval for background changes (more frequent for demo)
        setInterval(() => {
            this.changeBackground();
        }, this.updateInterval);
    }
}

// Initialize the demo background manager when the DOM is loaded
document.addEventListener('DOMContentLoaded', () => {
    window.demoBackgroundManager = new DemoBackgroundManager();
});
