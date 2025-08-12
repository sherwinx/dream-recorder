/**
 * Dream Recorder Demo
 * Cycles through logo, generating icon, clock, and recording icon
 */

class DemoController {
    constructor() {
        this.elements = {
            logo: document.getElementById('logo'),
            generatingIcon: document.getElementById('generating-icon'),
            clockDisplay: document.getElementById('clockDisplay'),
            recordingIcon: document.getElementById('recording-icon')
        };
        
        this.displayDuration = 5000; // 5 seconds per element
        this.fadeDuration = 1000; // 1 second fade in/out
        
        this.sequence = [
            'logo',
            'generatingIcon',
            'clockDisplay',
            'recordingIcon'
        ];
        
        this.currentIndex = 0;
        
        // Initialize random clock time
        this.setRandomClockTime();
        
        // Start the demo cycle
        this.startCycle();
    }
    
    setRandomClockTime() {
        // Generate random hours (0-23) and minutes (0-59)
        const hours = Math.floor(Math.random() * 24);
        const minutes = Math.floor(Math.random() * 60);
        
        // Update clock display
        const hourTens = Math.floor(hours / 10);
        const hourOnes = hours % 10;
        const minuteTens = Math.floor(minutes / 10);
        const minuteOnes = minutes % 10;
        
        document.querySelector('.hour-tens').textContent = hourTens;
        document.querySelector('.hour-ones').textContent = hourOnes;
        document.querySelector('.minute-tens').textContent = minuteTens;
        document.querySelector('.minute-ones').textContent = minuteOnes;
    }
    
    async fadeIn(element) {
        return new Promise(resolve => {
            element.classList.add('visible');
            setTimeout(resolve, this.fadeDuration);
        });
    }
    
    async fadeOut(element) {
        return new Promise(resolve => {
            element.classList.remove('visible');
            setTimeout(resolve, this.fadeDuration);
        });
    }
    
    async showElement(elementKey) {
        const element = this.elements[elementKey];
        if (!element) return;
        
        // Special handling for clock - set new random time each cycle
        if (elementKey === 'clockDisplay') {
            this.setRandomClockTime();
        }
        
        // Fade in
        await this.fadeIn(element);
        
        // Display for duration
        await new Promise(resolve => setTimeout(resolve, this.displayDuration));
        
        // Fade out
        await this.fadeOut(element);
    }
    
    async startCycle() {
        while (true) {
            // Get current element to show
            const elementKey = this.sequence[this.currentIndex];
            
            // Show the element
            await this.showElement(elementKey);
            
            // Move to next element in sequence
            this.currentIndex = (this.currentIndex + 1) % this.sequence.length;
            
            // Small delay between elements
            await new Promise(resolve => setTimeout(resolve, 500));
        }
    }
}

// Initialize when DOM is loaded
document.addEventListener('DOMContentLoaded', () => {
    // Initialize Socket.IO connection (even though we don't use it for the demo)
    const socket = io();
    
    socket.on('connect', () => {
        console.log('Connected to demo server');
    });
    
    // Start the demo controller
    const demo = new DemoController();
});
