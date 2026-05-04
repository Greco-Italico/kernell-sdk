package main

import (
	"log"
	"sync"
	"time"

	"github.com/libp2p/go-libp2p/core/host"
	"github.com/libp2p/go-libp2p/core/peer"
)

type PeerScore struct {
	Score       float64
	LastUpdate  time.Time
	BannedUntil time.Time
}

var peerScores = make(map[string]*PeerScore)
var peerMsgCount = make(map[string]int)
var mu sync.Mutex

const (
	BanThreshold   = -100.0
	DecayFactor    = 0.98
	BanDuration    = 5 * time.Minute
)

func applyPeerPenalty(globalHost host.Host, peerID string, delta float64, ban bool) {
	mu.Lock()
	defer mu.Unlock()

	ps, exists := peerScores[peerID]
	if !exists {
		ps = &PeerScore{Score: 0}
		peerScores[peerID] = ps
	}

	ps.Score += delta
	ps.LastUpdate = time.Now()

	log.Printf("Peer %s score updated: %.2f\n", peerID, ps.Score)

	if ban || ps.Score <= BanThreshold {
		ps.BannedUntil = time.Now().Add(BanDuration)
		log.Printf("🚫 Peer %s banned until %s\n", peerID, ps.BannedUntil)
		
		// Drop connection asynchronously to avoid deadlocks
		go disconnectPeer(globalHost, peerID)
	}
}

func isPeerBanned(peerID string) bool {
	mu.Lock()
	defer mu.Unlock()

	ps, exists := peerScores[peerID]
	if !exists {
		return false
	}

	return time.Now().Before(ps.BannedUntil)
}

func disconnectPeer(h host.Host, peerID string) {
	p, err := peer.Decode(peerID)
	if err != nil {
		return
	}

	log.Println("🔌 Disconnecting peer:", peerID)
	h.Network().ClosePeer(p)
}

func trackMessage(peerID string) bool {
	mu.Lock()
	defer mu.Unlock()

	peerMsgCount[peerID]++

	if peerMsgCount[peerID] > 20 {
		return false // spam
	}
	return true
}

func startDecayLoop() {
	go func() {
		for {
			time.Sleep(30 * time.Second)

			mu.Lock()
			for _, ps := range peerScores {
				// Decay score towards 0
				ps.Score *= DecayFactor
			}
			mu.Unlock()
		}
	}()
}

func resetCountersLoop() {
	go func() {
		for {
			time.Sleep(10 * time.Second)
			mu.Lock()
			peerMsgCount = make(map[string]int)
			mu.Unlock()
		}
	}()
}
